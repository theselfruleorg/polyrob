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

from dataclasses import dataclass, field
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
    #: What ``install`` recorded (``old_sha``/``old_branch``) — 077's boot step
    #: keeps it to roll back new code that never starts.
    state: dict = field(default_factory=dict)
    #: 098 CL-09: read the commit to return to BEFORE ``install`` mutates, so
    #: the phase journal holds it on disk while the install runs.
    prepare: Optional[Callable[[], dict]] = None


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
    journal=None,
) -> ApplyResult:
    """Apply an update atomically; auto-rollback on any failure. Never raises for a step
    failure (returns an ``ApplyResult``); only a failure to take the master snapshot raises
    (we must never mutate unguarded).

    098 CL-09: with a ``journal`` (:class:`core.state.journal.PhaseJournal`) each
    phase is written BEFORE its step, with the snapshot name and the commit to
    return to — a kill (SIGKILL, a reboot) leaves an open journal that the next
    ``polyrob update`` or start rolls back (``cli.update.journal``)."""
    if journal is not None:
        import time as _t
        from core.state.proc import process_identity
        journal.begin("update", owner=process_identity().encode(),
                      rollback_info={"from_version": from_version, "to_version": to_version,
                                     "started_at": _t.time()}, force=True)
        journal.phase("snapshot")
    snap = create_snapshot(
        snapshots_root=ctx.snapshots_root, data_home=ctx.data_home,
        db_paths=ctx.db_paths or None, config_paths=ctx.config_paths,
        dir_paths=ctx.dir_paths, from_version=from_version, to_version=to_version,
        label="pre-update", timestamp=timestamp,
    )

    # The master snapshot guards every step, including migration. A former
    # per-step "guarded migrate" restored the same snapshot a second time and
    # a failed inner restore skipped code recovery altogether; one restore now.
    if journal is not None:
        prepared = dict(runners.prepare() or {}) if runners.prepare else {}
        journal.phase("prepared", snapshot=snap.name, **prepared)
    for step, run in (("install", runners.install), ("migrate", runners.migrate),
                      ("verify", runners.verify)):
        try:
            if journal is not None:
                journal.phase(step)
            run()
        except BaseException as exc:  # restore even on KeyboardInterrupt
            errors = []
            for name, undo in (("code", runners.rollback_code),
                               ("data", lambda: restore_snapshot(
                                   snap.path, data_home=ctx.data_home,
                                   allowed_paths=[*ctx.config_paths, *ctx.dir_paths, *ctx.db_paths]))):
                try:
                    undo()
                except BaseException as rollback_exc:
                    errors.append(f"{name}: {rollback_exc}")
            if journal is not None:
                _journal_end(journal, errors, f"{step}: {exc}")
            return ApplyResult(False, step, exc, snap, not errors, tuple(errors))

    if journal is not None:
        journal.done(phase="applied")
    return ApplyResult(True, None, None, snap, False)


def _journal_end(journal, errors, error: str) -> None:
    """Close the journal after an in-process rollback: ``rolled_back`` (done)
    or ``rollback_failed`` (failed — it blocks the next apply)."""
    try:
        if errors:
            journal.phase("rollback_failed", errors=list(errors))
            journal.failed(f"{error}; rollback: {'; '.join(errors)}")
        else:
            journal.done(phase="rolled_back")
    except Exception:
        pass
