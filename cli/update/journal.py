"""The update phase journal and its recovery (098 CL-09).

``polyrob update --apply`` (the direct path) writes a
:class:`core.state.journal.PhaseJournal` at ``<snapshots_root>/update.journal.json``
before each phase: the snapshot name and the commit to return to are on disk
while ``git``/``pip`` run. A kill the in-process rollback cannot see (SIGKILL,
a reboot, a dropped SSH session) leaves the journal ``running``; the next
``polyrob update`` (any verb) or the next start of the service rolls the
install back — but only when the journal's owner process is gone.

A rollback that itself failed (``rollback_failed``) blocks the next apply until
``--rollback`` or ``--force``: applying over a torn install makes it worse.
"""
from __future__ import annotations

import contextlib
import logging
import signal
from pathlib import Path
from typing import Callable, Optional

from core.state.journal import PhaseJournal

logger = logging.getLogger(__name__)

JOURNAL_NAME = "update.journal.json"


def update_journal(snapshots_root) -> PhaseJournal:
    return PhaseJournal(str(Path(snapshots_root) / JOURNAL_NAME))


def recover_open_update(snapshots_root, *, rollback: Optional[Callable[[dict], dict]] = None,
                        echo: Callable[[str], None] = print) -> Optional[str]:
    """Roll back an update whose process died mid-apply. Returns
    ``"rolled_back"``, ``"rollback_failed"``, ``"live"`` (its process still
    runs) or None (nothing open). Never raises."""
    try:
        journal = update_journal(snapshots_root)
        entry = journal.pending()
        if entry is None:
            return None
        from core.state.proc import ProcIdentity, is_alive
        owner = ProcIdentity.decode(entry.get("owner"))
        if owner is not None and is_alive(owner):
            return "live"
        info = dict(entry.get("info") or {})
        if entry.get("phase") in ("begin", "snapshot"):
            # Nothing mutated yet: the snapshot may be partial, the code untouched.
            journal.done(phase="abandoned")
            return "rolled_back"
        if rollback is None:
            from cli.commands.update import rollback_for_boot as rollback
        echo(f"[polyrob] an update was interrupted at the '{entry.get('phase')}' step — "
             "rolling it back")
        res = rollback({"old_sha": info.get("old_sha") or "",
                        "old_branch": info.get("old_branch") or "HEAD",
                        "snapshot": info.get("snapshot") or "",
                        "started_at": (entry.get("rollback") or {}).get("started_at") or 0})
        if res.get("ok"):
            journal.done(phase="rolled_back")
            return "rolled_back"
        journal.phase("rollback_failed", errors=list(res.get("errors") or []))
        journal.failed("; ".join(res.get("errors") or []) or "rollback failed")
        echo(f"[polyrob] the rollback did not complete: {res.get('errors')}. Snapshot "
             f"{info.get('snapshot') or '(none)'}; run `polyrob update --rollback` "
             f"and `git reset --keep {info.get('old_sha') or '<old commit>'}`.")
        return "rollback_failed"
    except Exception:
        logger.warning("update journal recovery failed", exc_info=True)
        return None


def failed_rollback(snapshots_root) -> Optional[dict]:
    """The journal entry of an update whose rollback failed, or None."""
    entry = update_journal(snapshots_root).read()
    if entry and entry.get("state") == "failed" and entry.get("phase") == "rollback_failed":
        return entry
    return None


def clear_failed(snapshots_root) -> None:
    """``--rollback`` / ``--force`` acknowledged a failed rollback."""
    with contextlib.suppress(Exception):
        update_journal(snapshots_root).clear()


@contextlib.contextmanager
def shield_signals():
    """Defer SIGTERM/SIGHUP while the apply (and its own rollback) runs: a
    stop or a dropped SSH session mid-``git checkout``/``pip install`` left a
    torn tree. The deferred signal exits right after."""
    got = []
    old = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, lambda *_a: got.append(True))
    except (ValueError, OSError, AttributeError):  # not the main thread
        pass
    try:
        yield
    finally:
        for sig, handler in old.items():
            with contextlib.suppress(Exception):
                signal.signal(sig, handler)
        if got:
            raise SystemExit(1)
