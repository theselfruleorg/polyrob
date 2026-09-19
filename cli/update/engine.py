"""The apply spine for `polyrob update`.

Orchestrates the 7-step update as an **atomic, always-rollbackable** operation:

    snapshot(full) → install(code) → migrate → verify → [auto-rollback on any failure]

Every mutating step is an injected ``runner`` so the ordering + rollback logic is fully
unit-testable without touching a real install; the real runners (git pull / pip install /
`migrations.migrate upgrade` / smoke-import) live in :mod:`cli.update.runners`.

Invariants (never violated):
- A full snapshot (DBs + config + identity + skills) is taken BEFORE anything mutates.
- The migration runs under the SAME full snapshot as every other step, so a
  half-applied schema is restored byte-identical (one snapshot, one restore).
- ANY step failure → revert the code AND restore the pre-update snapshot, then report the
  failed step. The caller never ends up with new code on an old DB (or vice-versa).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from cli.update.context import UpdateContext
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
    rollback_errors: tuple[str, ...] = ()


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

    # The master snapshot guards every step, including migration. A former
    # per-step "guarded migrate" restored the same snapshot a second time and
    # a failed inner restore skipped code recovery altogether; one restore now.
    for step, run in (("install", runners.install), ("migrate", runners.migrate),
                      ("verify", runners.verify)):
        try:
            run()
        except BaseException as exc:  # restore even on KeyboardInterrupt
            errors = []
            for name, undo in (("code", runners.rollback_code),
                               ("data", lambda: restore_snapshot(snap.path))):
                try:
                    undo()
                except BaseException as rollback_exc:
                    errors.append(f"{name}: {rollback_exc}")
            return ApplyResult(False, step, exc, snap, not errors, tuple(errors))

    return ApplyResult(True, None, None, snap, False)
