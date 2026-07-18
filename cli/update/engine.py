"""The apply spine for `polyrob update`.

Orchestrates the 7-step update as an **atomic, always-rollbackable** operation:

    snapshot(full) → install(code) → migrate(guarded) → verify → [auto-rollback on any failure]

Every mutating step is an injected ``runner`` so the ordering + rollback logic is fully
unit-testable without touching a real install; the real runners (git pull / pip install /
`migrations.migrate upgrade` / smoke-import) live in :mod:`cli.update.runners`.

Invariants (never violated):
- A full snapshot (DBs + config + identity + skills) is taken BEFORE anything mutates.
- The migration runs guarded (:func:`cli.update.migrate_guarded.migrate_guarded`) so a
  half-applied schema is restored byte-identical.
- ANY step failure → revert the code AND restore the pre-update snapshot, then report the
  failed step. The caller never ends up with new code on an old DB (or vice-versa).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from cli.update.context import UpdateContext
from cli.update.migrate_guarded import migrate_guarded
from cli.update.snapshot import SnapshotInfo, create_snapshot, restore_snapshot


@dataclass
class UpdateRunners:
    """The four mutating steps, injected. Each raises on failure."""
    install: Callable[[], None]         # fetch + install the new code
    migrate: Callable[[], None]         # apply DB migrations
    verify: Callable[[], None]          # smoke-check the new install (raise if broken)
    rollback_code: Callable[[], None]   # revert code to the previous version


@dataclass
class ApplyResult:
    ok: bool
    failed_step: Optional[str]          # "install" | "migrate" | "verify" | None
    error: Optional[BaseException]
    snapshot: Optional[SnapshotInfo]
    rolled_back: bool


def apply_update(
    *,
    ctx: UpdateContext,
    runners: UpdateRunners,
    from_version: str,
    to_version: str = "",
    timestamp: Optional[str] = None,
) -> ApplyResult:
    """Apply an update atomically; auto-rollback on any failure. Never raises for a step
    failure (returns an ``ApplyResult``); only a failure to take the master snapshot raises
    (we must never mutate unguarded)."""
    snap = create_snapshot(
        snapshots_root=ctx.snapshots_root, data_home=ctx.data_home,
        db_paths=ctx.db_paths or None, config_paths=ctx.config_paths,
        dir_paths=ctx.dir_paths, from_version=from_version, to_version=to_version,
        label="pre-update", timestamp=timestamp,
    )

    def _unwind() -> None:
        try:
            runners.rollback_code()
        except Exception:
            pass  # best-effort code revert; the data restore below is the load-bearing part
        restore_snapshot(snap.path)

    # 1. install new code
    try:
        runners.install()
    except BaseException as exc:  # noqa: BLE001
        _unwind()
        return ApplyResult(False, "install", exc, snap, True)

    # 2. guarded migration (restores DBs byte-identical on its own failure).
    # Reuses the master snapshot — one snapshot per apply, and rollback selection
    # always finds the FULL one (U2/U9).
    mres = migrate_guarded(
        migrate=runners.migrate, db_paths=ctx.db_paths,
        snapshots_root=ctx.snapshots_root, data_home=ctx.data_home,
        from_version=from_version, to_version=to_version, snapshot=snap,
    )
    if not mres.ok:
        _unwind()
        return ApplyResult(False, "migrate", mres.error, snap, True)

    # 3. verify the new install boots
    try:
        runners.verify()
    except BaseException as exc:  # noqa: BLE001
        _unwind()
        return ApplyResult(False, "verify", exc, snap, True)

    return ApplyResult(True, None, None, snap, False)
