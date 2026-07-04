"""Idempotent, fail-open schema migration at process start (C3).

The live boot path creates the base schema inline (``modules/database/connection.py``)
at HEAD, so the semver migration framework was never engaged — meaning a NEW migration
(e.g. a 0.5.0 ``ADD COLUMN``) would never auto-apply and the app would hit
``no such column`` at runtime on an upgraded DB. This runs pending migrations once at
boot:

- **First engagement** (``schema_versions`` empty): the inline schema already produced
  the HEAD shape, so every shipped migration is *stamped* as applied WITHOUT executing
  it (replaying its ``CREATE``/``ALTER`` on an at-head DB would duplicate-error). Only
  migrations added AFTER this baseline execute on later boots.
- **Subsequent boots**: only genuinely-pending (future) migrations run. Each migration
  body is idempotent (``IF NOT EXISTS`` / guarded ``ALTER``); we record via the tracking
  SSOT (``schema_versions``) with an is-applied guard so a self-recording migration can't
  double-insert. (Note: ``v1_0_0_baseline`` self-records to the *legacy* ``schema_version``
  table, so relying on migrations to self-record is not safe — the SSOT record here is.)
- ``on_before_change()`` fires once, ONLY when a pending migration is about to execute
  (the snapshot-before-migrate hook, C2) — never on a no-op boot.

**Never raises**: a failure logs loudly and leaves the DB on the inline schema (today's
behavior), so boot cannot regress. A best-effort single-flight file lock guards
concurrent boots (``workers>1``); if it can't be taken, another process is migrating and
this call is a no-op.

Scope: the main ``bot.db``. The sidecar DBs (memory/goals/cron/skill_usage/users/
tg_dedup — see ``core/db_manifest``) self-manage their schema via ``CREATE TABLE IF NOT
EXISTS`` and stay **additive-only** (new tables/columns via ``IF NOT EXISTS``) until a
per-DB versioning slice lands; they are outside this runner.
"""
from __future__ import annotations

import importlib.util
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from migrations.version_manager import DatabaseVersionManager, latest_migration_version

logger = logging.getLogger("migrations.boot")

_VERSIONS_DIR = Path(__file__).resolve().parent / "versions"


def _version_from_filename(path: Path) -> Optional[str]:
    parts = path.stem.split("_", 3)
    if len(parts) >= 3 and parts[0].startswith("v"):
        return f"{parts[0][1:]}.{parts[1]}.{parts[2]}"
    return None


def _shipped_migrations(versions_dir: Path) -> List[Tuple[str, Path]]:
    out: List[Tuple[str, Path]] = []
    for p in sorted(versions_dir.glob("v*.py")):
        v = _version_from_filename(p)
        if v:
            out.append((v, p))
    return out


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _BootLock:
    """Best-effort exclusive file lock. Never blocks; None path = no lock."""

    def __init__(self, lock_path: Optional[Path]):
        self._path = Path(lock_path) if lock_path else None
        self._fh = None
        self.acquired = False

    def __enter__(self):
        if self._path is None:
            self.acquired = True  # no lock requested → proceed
            return self
        try:
            import fcntl
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._path, "w")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except Exception:
            # Another process holds it (or no fcntl) — treat as "someone else is migrating".
            self.acquired = False
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


async def apply_migrations_at_boot(
    db,
    db_manager=None,
    *,
    versions_dir: Path = _VERSIONS_DIR,
    on_before_change: Optional[Callable[[], None]] = None,
    lock_path: Optional[Path] = None,
) -> dict:
    """Run pending migrations idempotently at boot. Never raises. Returns a summary."""
    summary = {
        "baselined": False, "applied": [], "pending": [],
        "error": None, "skipped_lock": False,
    }
    try:
        with _BootLock(lock_path) as lock:
            if not lock.acquired:
                summary["skipped_lock"] = True
                logger.info("migration-on-boot: another process holds the lock — skipping")
                return summary

            version_mgr = DatabaseVersionManager(db)
            await version_mgr.initialize()
            current = await version_mgr.get_current_version()
            shipped = _shipped_migrations(versions_dir)

            if current is None:
                # First engagement: inline schema is already at HEAD → stamp, don't execute.
                for version, path in shipped:
                    if not await version_mgr.is_version_applied(version):
                        await version_mgr.record_migration(
                            version, f"baseline (stamped at boot): {path.stem}")
                summary["baselined"] = True
                summary["applied"] = [v for v, _ in shipped]
                logger.info(
                    "schema baselined at HEAD (%s) — %d version(s) stamped, none executed",
                    latest_migration_version(versions_dir), len(shipped))
                return summary

            pending = await version_mgr.get_pending_migrations(versions_dir)
            summary["pending"] = [p.name for p in pending]
            if not pending:
                logger.debug("migration-on-boot: up to date (%s)", current)
                return summary

            # A real schema change is imminent — snapshot first (C2).
            if on_before_change is not None:
                try:
                    on_before_change()
                except Exception as exc:
                    logger.warning("pre-migration snapshot hook failed (continuing): %s", exc)

            for path in pending:
                module = _load_module(path)
                version = getattr(module, "VERSION", _version_from_filename(path))
                start = time.time()
                await module.upgrade(db, db_manager)
                # Record via the SSOT exactly once (migrations self-record inconsistently).
                if version and not await version_mgr.is_version_applied(version):
                    await version_mgr.record_migration(
                        version, getattr(module, "DESCRIPTION", path.stem),
                        int((time.time() - start) * 1000))
                summary["applied"].append(version)
                logger.info("applied migration %s (%s)", version, path.name)
            return summary
    except Exception as exc:  # never let a migration failure crash boot
        logger.error(
            "migration-on-boot failed (leaving DB on inline schema): %s", exc, exc_info=True)
        summary["error"] = str(exc)
        return summary


async def run_boot_migrations(container, *, local: bool = True) -> dict:
    """Fail-open wiring: migrate the container's main DB (bot.db) at boot, snapshotting
    first if a real schema change is pending (C2). Resolves paths from the live runtime.

    Never raises — any wiring/resolution failure logs and returns without touching boot.
    Where a container has no ``database_manager`` (e.g. the lightweight CLI container,
    which uses additive-only sqlite sidecars), this is a no-op.
    """
    try:
        get = getattr(container, "get_service", None)
        db_manager = get("database_manager") if callable(get) else None
        if not db_manager or getattr(db_manager, "connection", None) is None:
            logger.debug("run_boot_migrations: no database_manager/connection — skipping")
            return {"applied": [], "error": None, "skipped_lock": False, "no_db": True}
        db = db_manager.connection

        snapshots_root = data_home = None
        config_paths: list = []
        dir_paths: list = []
        try:
            from cli.update.context import resolve_update_context
            uctx = resolve_update_context(local=local)
            snapshots_root, data_home = uctx.snapshots_root, uctx.data_home
            config_paths, dir_paths = uctx.config_paths, uctx.dir_paths
        except Exception as exc:
            logger.debug("run_boot_migrations: update-context unavailable (%s) — no snapshot", exc)

        def _snapshot_before():
            if snapshots_root is None:
                return
            from cli.update.snapshot import create_snapshot, prune_snapshots
            from core.version import get_version
            create_snapshot(
                snapshots_root=snapshots_root, data_home=data_home,
                from_version=get_version(), method="boot-migrate",
                config_paths=config_paths, dir_paths=dir_paths, label="pre-migration")
            prune_snapshots(snapshots_root, keep=3)

        lock_path = (Path(data_home) / "migrate.lock") if data_home else None
        return await apply_migrations_at_boot(
            db, db_manager, on_before_change=_snapshot_before, lock_path=lock_path)
    except Exception as exc:
        logger.error("run_boot_migrations wiring failed (fail-open): %s", exc, exc_info=True)
        return {"applied": [], "error": str(exc)}
