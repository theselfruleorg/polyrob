"""Is the process that holds a goal claim still alive? (cold-start requeue guard)

The cold-start sweep (``GoalBoard.requeue_running_on_boot``) exists so a goal left
``running`` by a killed process comes back WITHOUT a failure increment — a deploy
is not the goal's fault. It must not, however, requeue a goal a DIFFERENT, still
live process is executing right now: every ``start_autonomy()`` runs the sweep, so
a second worker (or a ``rob`` REPL under ``POLYROB_LOCAL``) booting against the
same ``goals.db`` would otherwise rip a live claim, double-run the goal, and make
the original run's ``record_success`` CAS miss.

The dispatcher stamps its claim with ``goal-dispatch-<pid>``
(``GoalDispatcher._worker``), and ``goals.db`` is a single-host SQLite file, so
the claim identifies a LOCAL process. That makes "is the owner still there?"
answerable exactly, which is what lets the sweep keep its no-failure-increment
promise for a real restart while leaving a live claim alone.

Deliberately three-valued: ``None`` (undeterminable) is NOT ``False``. An
unrecognised claim string is treated as possibly-live by the caller — the
fail-safe direction, since the cost of waiting is one ``reclaim_stale`` TTL and
the cost of guessing wrong is a concurrent double-run.
"""
import os
import re
from typing import Optional

#: The dispatcher's claim format (``GoalDispatcher._worker``). A claim written in
#: any other shape is not attributable to a local pid.
_WORKER_PID_RE = re.compile(r"^goal-dispatch-(\d+)$")


def claim_worker_pid(claim_lock: Optional[str]) -> Optional[int]:
    """The local pid encoded in *claim_lock*, or ``None`` if it encodes none."""
    m = _WORKER_PID_RE.match((claim_lock or "").strip())
    if not m:
        return None
    try:
        pid = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def claim_owner_alive(claim_lock: Optional[str]) -> Optional[bool]:
    """``True`` = the owning process is running, ``False`` = it is PROVABLY gone,
    ``None`` = undeterminable (unrecognised claim, or the probe itself failed).

    ⚠️ Same-host only: pid identity is meaningful because ``goals.db`` is a local
    SQLite file. A shared-filesystem, multi-host board would need a host-qualified
    claim; until then an unknown owner resolves to ``None`` (possibly-live).
    """
    pid = claim_worker_pid(claim_lock)
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False          # provably gone
    except PermissionError:
        return True           # alive, owned by another user
    except OSError:
        return None           # probe failed — say "unknown", never "gone"
    return True


__all__ = ["claim_owner_alive", "claim_worker_pid"]
