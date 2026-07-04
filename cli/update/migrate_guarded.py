"""Snapshot-guarded DB migration — the atomicity anchor for an automated update.

A schema migration is the one update step that can leave the system worse than it found
it: a half-applied `ALTER`/`ADD COLUMN` wedges the DB so the app won't boot, and rolling
back only the *code* leaves code and schema out of sync. :func:`migrate_guarded` snapshots
every DB (WAL-safe) BEFORE the migration and restores them **byte-identical** if it throws,
so a failed migration is a no-op, not a corrupt database.

The migration itself is injected (``migrate`` callable) so this is unit-testable without a
real schema change; in the engine it wraps ``python -m migrations.migrate upgrade``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from cli.update.snapshot import SnapshotInfo, create_snapshot, restore_snapshot


@dataclass
class MigrateResult:
    ok: bool
    snapshot: Optional[SnapshotInfo]
    error: Optional[BaseException] = None
    restored: bool = False


def migrate_guarded(
    *,
    migrate: Callable[[], None],
    db_paths: List[Path],
    snapshots_root: Path,
    data_home: Path,
    from_version: str,
    to_version: str = "",
    timestamp: Optional[str] = None,
) -> MigrateResult:
    """Snapshot the DBs, run ``migrate()``; restore byte-identical on failure.

    Never raises for a migration failure — returns ``MigrateResult(ok=False, ...)`` after
    restoring, so the caller (the update engine) can decide how to unwind the rest. A
    failure to even take the pre-migrate snapshot IS raised (we must not migrate unguarded).
    """
    snap = create_snapshot(
        snapshots_root=snapshots_root, data_home=data_home, db_paths=db_paths,
        from_version=from_version, to_version=to_version, label="pre-migrate",
        timestamp=timestamp,
    )
    try:
        migrate()
    except BaseException as exc:  # noqa: BLE001 — restore on ANY failure, incl. KeyboardInterrupt
        restore_snapshot(snap.path)
        return MigrateResult(ok=False, snapshot=snap, error=exc, restored=True)
    return MigrateResult(ok=True, snapshot=snap)
