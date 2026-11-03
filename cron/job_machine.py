"""The cron job machine (098 S1, MO-09) and the run lease.

``cron_jobs.status`` is the state; ``enabled`` is derived from it (0 for
``done``/``failed``/``cancelled``) and written only beside a status move. A
``running`` row names its owner (``run_owner`` = the claiming process's
identity, 098 S5) and a lease (``lease_until``, unix seconds): the reaper takes
a row only when its lease ran out or its owner process is provably gone, so a
stolen tick lock can no longer reset a live run and run it twice.
"""
from __future__ import annotations

import time
from typing import Optional

from core.state.machine import StateMachine

CRON_JOB_MACHINE = StateMachine(
    name="cron_job",
    states=frozenset({"scheduled", "running", "paused", "done", "failed", "cancelled"}),
    transitions={
        "scheduled": frozenset({"running", "paused", "cancelled"}),
        "running": frozenset({"scheduled", "done", "failed", "paused", "cancelled"}),
        "paused": frozenset({"scheduled", "cancelled"}),
        "done": frozenset({"cancelled"}),
        "failed": frozenset({"cancelled"}),
    },
    terminal=frozenset({"cancelled"}),
    initial=frozenset({"scheduled", "paused"}),
)

#: Slack past the run's own cap before a lease counts as expired.
LEASE_SLACK_SECONDS = 120


def run_owner() -> str:
    from core.state.proc import process_identity
    return process_identity().encode()


def lease_is_live(owner: Optional[str], lease_until: Optional[float],
                  now: Optional[float] = None) -> Optional[bool]:
    """True = a live run holds the row; False = provably orphaned (lease out,
    or owner process gone); None = a legacy row with no lease (only the tick
    lock can judge it)."""
    if lease_until is None and not owner:
        return None
    now = time.time() if now is None else now
    if lease_until is not None and float(lease_until) < now:
        return False
    if owner:
        from core.state.proc import ProcIdentity, is_alive
        ident = ProcIdentity.decode(owner)
        if ident is not None and not is_alive(ident):
            return False
    return True


__all__ = ["CRON_JOB_MACHINE", "LEASE_SLACK_SECONDS", "lease_is_live", "run_owner"]
