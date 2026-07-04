"""Self-wake rail (W1, Reference-parity §self-wake).

POLYROB re-enters a session only on a *user* message — there is no way for a finished
background job, goal run, or cron task to forge a fresh internal turn. UP-12 added
ONE re-entry path for async delegations (`orchestrator._deliver_async_delegation`
→ `submit_user_message(kind="delegation_result")` → HITL queue → run-loop drain),
but it (a) only re-enters a session whose loop is *already running* and (b) has no
re-entry **depth** cap, so a self-triggering loop could ping-pong indefinitely.

This module supplies the two genuinely-new pieces, leaving UP-12's proven rail in
place rather than building a second queue:

- :class:`ReentryBudget` — a per-session depth + idle-backoff guard. Producers call
  ``allow(session_id)`` before forging a wake and ``record(session_id)`` after; a
  genuine user turn calls ``reset(session_id)``. This is the runaway/cost guard.
- :func:`format_self_wake` — wraps forged text in UP-06 ``<untrusted_tool_result>``
  delimiters so a completed job's output is read as DATA, never as instructions.

**Injection path (be precise):** ``deliver_self_wake`` routes the UP-06-wrapped text
through the SAME proven ingress UP-12 uses — ``submit_user_message`` → HITL queue →
``inject_user_guidance`` — so a forged turn enters as a continuation user-guidance
message (NOT a typed ``SYSTEM_NOTE`` control message; that path is reserved for the
in-history control injections like memory prefetch). The UP-06 wrapper is therefore the
operative safety framing on this rail, and it is always applied. The ``metadata`` carries
``source="self_wake"``/``kind="self_wake"`` so the turn is auditable as non-user-originated.

Everything here is pure and process-local (self-wake is resident-orchestrator-only,
single-worker scope — same constraint UP-12 already lives under). Gated by
``SELF_WAKE_ENABLED`` at the call sites; this module is inert until a producer runs.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

SELF_WAKE_KIND = "self_wake"
DELEGATION_RESULT_KIND = "delegation_result"

# SK-F10 (T11 followup): the kinds a producer uses to forge a non-user re-entry
# into a session (self-wake W1, async-delegation-result UP-12). This module has
# zero project-internal imports (stdlib only), which is exactly why it is the
# shared home: the ingress producer (agents.task.agent.core.user_ingress), the
# async-delegation producer (agents.task.agent.orchestrator), and the
# security-gate consumer (tools.controller.action_registration) can all import
# these constants without risking an import cycle. Previously duplicated as
# independent literal tuples/strings that had to be hand-kept in sync — a single
# miss on any side would silently de-gate the forged-turn check.
FORGED_TURN_KINDS = (SELF_WAKE_KIND, DELEGATION_RESULT_KIND)


@dataclass
class _SessionBudget:
    count: int = 0
    last_wake_at: float = 0.0


class ReentryBudget:
    """Bounds how often a session may be re-entered by a forged (non-user) turn.

    Two limits, both per-session:
      * ``max_reentries`` — total forged wakes allowed before a genuine user turn
        resets the counter (prevents an unbounded self-triggering loop);
      * ``idle_backoff_s`` — minimum spacing between consecutive forged wakes
        (prevents a tight wake storm).

    Thread-safe (producers may run on aux/daemon threads). Process-local by design.
    """

    def __init__(self, max_reentries: int, idle_backoff_s: float,
                 *, clock: Callable[[], float] = time.monotonic):
        self._max = max(0, int(max_reentries))
        self._backoff = max(0.0, float(idle_backoff_s))
        self._clock = clock
        self._lock = threading.Lock()
        self._by_session: Dict[str, _SessionBudget] = {}

    def allow(self, session_id: str) -> bool:
        """True if a forged wake for ``session_id`` is within budget right now."""
        if self._max <= 0:
            return False
        with self._lock:
            b = self._by_session.get(session_id)
            if b is None:
                return True
            if b.count >= self._max:
                return False
            if self._backoff and (self._clock() - b.last_wake_at) < self._backoff:
                return False
            return True

    def record(self, session_id: str) -> None:
        """Account a forged wake that was just dispatched."""
        with self._lock:
            b = self._by_session.setdefault(session_id, _SessionBudget())
            b.count += 1
            b.last_wake_at = self._clock()

    def try_consume(self, session_id: str) -> bool:
        """Atomic allow()+record(): True (and consumes a slot) iff within budget.

        Use this instead of a separate allow()/record() pair — those span two lock
        acquisitions, so two concurrent producers could both pass allow() before
        either record()s and exceed max_reentries by one (a TOCTOU off-by-one).
        """
        if self._max <= 0:
            return False
        with self._lock:
            b = self._by_session.get(session_id)
            if b is None:
                b = self._by_session.setdefault(session_id, _SessionBudget())
            elif b.count >= self._max:
                return False
            elif self._backoff and (self._clock() - b.last_wake_at) < self._backoff:
                return False
            b.count += 1
            b.last_wake_at = self._clock()
            return True

    def reset(self, session_id: str) -> None:
        """Clear the budget for a session (call when a genuine user turn arrives)."""
        with self._lock:
            self._by_session.pop(session_id, None)

    def remaining(self, session_id: str) -> int:
        with self._lock:
            b = self._by_session.get(session_id)
            used = b.count if b else 0
            return max(0, self._max - used)


# --- process-local singleton (resident-only scope, like UP-12) ---------------

_BUDGET_LOCK = threading.Lock()
_BUDGET_SINGLETON: Optional[ReentryBudget] = None


def get_reentry_budget() -> ReentryBudget:
    """Return the process-wide ReentryBudget, built from env on first use.

    Reads SELF_WAKE_MAX_REENTRIES / SELF_WAKE_IDLE_BACKOFF_SEC via AutonomyConfig.
    """
    global _BUDGET_SINGLETON
    with _BUDGET_LOCK:
        if _BUDGET_SINGLETON is None:
            from agents.task.constants import AutonomyConfig
            _BUDGET_SINGLETON = ReentryBudget(
                AutonomyConfig.self_wake_max_reentries(),
                AutonomyConfig.self_wake_idle_backoff_sec(),
            )
        return _BUDGET_SINGLETON


def reset_reentry_budget() -> None:
    """Test seam: drop the singleton so env changes take effect."""
    global _BUDGET_SINGLETON
    with _BUDGET_LOCK:
        _BUDGET_SINGLETON = None


def format_self_wake(text: str, *, source: str = "self_wake") -> str:
    """Frame forged wake text as untrusted DATA for safe injection.

    The returned string is what gets queued as the forged turn body. It is wrapped
    in UP-06 untrusted-result delimiters so an injected directive inside a completed
    job's output cannot hijack the agent.
    """
    try:
        from agents.task.agent.core.untrusted_wrap import wrap_untrusted
        return wrap_untrusted(source, text)
    except Exception:
        return text  # fail-open: framing is defense-in-depth, not a hard dependency
