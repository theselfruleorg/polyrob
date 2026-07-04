"""lifecycle.py — the CLI's single source of truth for "is a turn active, and
for how long".

Before this object the CLI had four uncoordinated notions of session time/status
(session-age clock shown as "elapsed", an ad-hoc ``status`` string driven by feed
events, a correct-but-unused per-turn clock, and the agent-side ``SessionStatus``).
``TurnLifecycle`` collapses them into one model the renderer/status-bar derive from.

Design (docs/plans/2026-06-26-session-runtime-lifecycle-PLAN.md §1, Fusion-validated):

**Two orthogonal lanes** — a background autonomy turn (cron/goal/self-wake) can run
*while* the user's foreground turn runs, so one scalar phase can't hold both:

- **foreground** — user turns. Owns the work clock (``active_elapsed``) and the
  primary status word. Driven ONLY by the turn runner via ``begin_turn``/``end_turn``.
- **autonomy** — background turns. A simple in-flight counter that drives a muted
  ``⟲ autonomy`` indicator. NEVER touches the work clock or the status word.

**Invariant:** feed events (``Step``/``LLMCall``/``ToolExec``/…) NEVER mutate this
object — only the turn seams do. That is what makes "running while idle" impossible
by construction (a stray/background feed event can't flip the foreground lane) and
makes a post-cancel straggler event harmless.

**Turn token** — ``begin_turn`` returns a monotonically-increasing token; ``end_turn``
is a no-op unless handed the *current* token. This defeats the Ctrl-C→immediate-resubmit
race where a cancelled task's late ``end_turn`` would otherwise settle the *new* turn.

The clock is injected so timing is deterministic in tests.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Callable


class Phase(Enum):
    """Foreground-lane phase."""

    IDLE = "idle"      # no user turn in flight (waiting for the user / follow-up)
    ACTIVE = "active"  # a user turn is running (between begin_turn and end_turn)


class TurnOutcome(Enum):
    """How a foreground turn ended (sticky on the bar until the next turn)."""

    NONE = "none"           # no turn yet, or last turn succeeded
    OK = "ok"               # passed to end_turn for a clean finish (→ NONE on idle)
    ERROR = "error"         # the turn raised
    CANCELLED = "cancelled" # the turn was interrupted (Ctrl-C)


#: Derived status words. ``working`` while ACTIVE; on IDLE the last outcome decides.
_OUTCOME_WORD = {
    TurnOutcome.NONE: "ready",
    TurnOutcome.OK: "ready",
    TurnOutcome.ERROR: "error",
    TurnOutcome.CANCELLED: "stopped",
}


class TurnLifecycle:
    """Single source of truth for the CLI's active-turn + work-clock + status word.

    Args:
        clock: Monotonic clock (injectable for tests). Defaults to ``time.monotonic``.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # Foreground lane.
        self.phase: Phase = Phase.IDLE
        self.active_started_at: float | None = None
        self.last_outcome: TurnOutcome = TurnOutcome.NONE
        self._token: int = 0
        # Autonomy lane (orthogonal): count of in-flight background turns.
        self._autonomy_active: int = 0

    # ------------------------------------------------------------------
    # Foreground lane — user turns (called from the turn runner ONLY)
    # ------------------------------------------------------------------

    def begin_turn(self) -> int:
        """Start a user turn → ACTIVE; the work clock starts at 0 and runs.

        Idempotent: calling it again while already ACTIVE is a no-op that returns
        the SAME token and does NOT reset the clock (defends against a duplicate
        submit/race). Clears any sticky error/cancel outcome.

        Returns the turn token to hand back to ``end_turn``.
        """
        if self.phase is Phase.ACTIVE:
            return self._token  # already running — don't restart the clock
        self._token += 1
        self.phase = Phase.ACTIVE
        self.active_started_at = self._clock()
        self.last_outcome = TurnOutcome.NONE
        return self._token

    def end_turn(self, token: int, outcome: TurnOutcome = TurnOutcome.OK) -> None:
        """End the user turn → IDLE; the work clock freezes (reads 0 on idle).

        No-op unless *token* is the current turn's token AND a turn is active —
        so a stale (cancelled-then-resubmitted) task's late settle can't touch the
        new turn, and a double end_turn is harmless. ``OK`` settles to ``ready``;
        ``ERROR``/``CANCELLED`` are sticky until the next ``begin_turn``.
        """
        if self.phase is not Phase.ACTIVE or token != self._token:
            return
        self.phase = Phase.IDLE
        self.active_started_at = None
        self.last_outcome = TurnOutcome.NONE if outcome is TurnOutcome.OK else outcome

    # ------------------------------------------------------------------
    # Autonomy lane — background turns (orthogonal to the foreground lane)
    # ------------------------------------------------------------------

    def begin_background(self) -> None:
        """A background (cron/goal/self-wake) turn started. Lights the muted
        autonomy indicator without touching the work clock or status word."""
        self._autonomy_active += 1

    def end_background(self) -> None:
        """A background turn finished. Floored at 0 (underflow-safe)."""
        self._autonomy_active = max(0, self._autonomy_active - 1)

    def autonomy_busy(self) -> bool:
        """True when one or more background turns are in flight."""
        return self._autonomy_active > 0

    # ------------------------------------------------------------------
    # Derived reads (the renderer / status bar consume these)
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """True when a user turn is running (drives the spinner + cooking line)."""
        return self.phase is Phase.ACTIVE

    def active_elapsed(self) -> float:
        """Work-clock seconds: ``now - active_started_at`` while ACTIVE, else 0.0.

        Frozen (0.0) on idle — this is the fix for the bar showing session age
        ("1h26m") while the user sits at the prompt.
        """
        if self.phase is Phase.ACTIVE and self.active_started_at is not None:
            return max(0.0, self._clock() - self.active_started_at)
        return 0.0

    def status_word(self) -> str:
        """The single derived status word: working / ready / error / stopped.

        ``running``-while-idle is impossible by construction: the word is
        ``working`` iff the foreground phase is ACTIVE, and the phase only goes
        ACTIVE via ``begin_turn``.
        """
        if self.phase is Phase.ACTIVE:
            return "working"
        return _OUTCOME_WORD.get(self.last_outcome, "ready")
