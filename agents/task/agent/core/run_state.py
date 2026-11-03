"""Owned per-run state of the Agent (W9 2026-10-10, structural review F4).

The Agent's 27 mixins used to share bare ``self`` attributes: ``_last_result`` was
assigned in nine mixin files and the LLM-call clock in three. Each group now has ONE
owner object, built at construction:

- ``agent.step_state`` (:class:`StepResultState`) — the last step's ``ActionResult``
  list, the input the run loop, loop detection and output validation all read.
- ``agent.llm_call`` (:class:`LLMCallState`) — whether an LLM request is in flight
  and since when (the stall watchdog reads it).

The old attribute names stay working: :func:`owned` installs a data descriptor on
``Agent`` that forwards ``agent._last_result`` / ``agent._llm_call_in_progress`` /
``agent._llm_call_start_time`` to the owner object (created lazily, so an
``Agent.__new__`` probe in a test still works). New code should read and write the
owner object directly.
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, List, Optional

from core.state.machine import StateMachine


@dataclass
class StepResultState:
    """The last step's results (a list of ``ActionResult``, or None before a step)."""

    last_result: Optional[List[Any]] = None

    def reset(self) -> None:
        """Forget the previous run's results (a new run must not see ``is_done``)."""
        self.last_result = None

    def is_done(self) -> bool:
        return bool(self.last_result) and any(
            getattr(r, "is_done", False) for r in self.last_result)

    def errors(self) -> List[str]:
        return [r.error for r in (self.last_result or []) if getattr(r, "error", None)]


@dataclass
class LLMCallState:
    """Whether an LLM request is in flight, and when it started (stall watchdog)."""

    in_progress: bool = False
    start_time: Optional[float] = None

    def begin(self, now: float) -> None:
        self.in_progress = True
        self.start_time = now

    def end(self) -> None:
        self.in_progress = False
        self.start_time = None

    @contextlib.contextmanager
    def active(self, now: float) -> Iterator["LLMCallState"]:
        """098 AG-19: ``begin`` on entry, ``end`` in ``finally`` — an exception or a
        cancel never leaves the stall watchdog blind (it skips while in flight)."""
        self.begin(now)
        try:
            yield self
        finally:
            self.end()


def llm_call_state(agent: Any) -> LLMCallState:
    """The agent's :class:`LLMCallState` (created on first use, as ``owned`` does)."""
    state = agent.__dict__.get("llm_call")
    if state is None:
        state = agent.__dict__["llm_call"] = LLMCallState()
    return state


#: 098 AG-01: the run lifecycle of one Agent. A run ends; the agent does not, so
#: no state is terminal: ``begin_run`` leaves ``stopped`` for the next run.
RUN_MACHINE = StateMachine(
    "agent_run",
    states=frozenset({"idle", "running", "paused", "stopped"}),
    transitions={
        "idle": frozenset({"running", "stopped"}),
        "running": frozenset({"paused", "stopped", "idle"}),
        "paused": frozenset({"running", "stopped", "idle"}),
        "stopped": frozenset({"running"}),
    },
    initial=frozenset({"idle"}),
)


@dataclass
class RunControl:
    """098 AG-01/02/03: the ONE owner of run-scoped control state.

    ``begin_run`` starts every run clean (a halt of the last run never wedges
    the next one); ``request_stop(reason)`` is the one writer of ``stopped`` and
    names why; ``pause``/``resume`` keep owner-pause time off the stall clock.
    Never persisted: a restored session starts ``idle``.
    """

    state: str = "idle"
    stop_reason: Optional[str] = None
    paused_since: Optional[float] = None
    paused_total: float = 0.0
    resumed_at: Optional[float] = None
    # AG-02: per-run counters (tool-free responses; whether this run acted)
    empty_action_counter: int = 0
    acted_this_run: bool = False

    def _move(self, to: str) -> bool:
        if self.state == to:
            return True
        if not RUN_MACHINE.check(self.state, to, ctx={"owner": "RunControl"}):
            return False
        self.state = to
        return True

    @property
    def stopped(self) -> bool:
        return self.state == "stopped"

    def begin_run(self) -> None:
        """Any state -> ``running``; clears the stop reason, the pause clock and
        the per-run counters."""
        if self.state == "paused":
            self.resume()
        self._move("running")
        self.stop_reason = None
        self.paused_since = None
        self.paused_total = 0.0
        self.resumed_at = None
        self.empty_action_counter = 0
        self.acted_this_run = False

    def request_stop(self, reason: Any = None) -> bool:
        """The one writer of ``stopped``. The first named reason wins (the exit
        that really ended the run). True when this call stopped the run."""
        value = getattr(reason, "value", reason)
        if value and not self.stop_reason:
            self.stop_reason = str(value)
        if self.state == "stopped":
            return False
        return self._move("stopped")

    def pause(self, now: Optional[float] = None) -> None:
        if self.state == "paused":
            return
        if self._move("paused"):
            self.paused_since = time.time() if now is None else now

    def resume(self, now: Optional[float] = None) -> float:
        """``paused`` -> ``running``; returns the paused seconds (0 when not paused)."""
        if self.state != "paused":
            return 0.0
        now = time.time() if now is None else now
        spent = max(0.0, now - (self.paused_since or now))
        self.paused_total += spent
        self.paused_since = None
        self.resumed_at = now
        self._move("running")
        return spent

    def end_run(self) -> None:
        """``running``/``paused`` -> ``idle``. A stopped run stays ``stopped`` so
        readers after the run see why it ended; the next ``begin_run`` clears it."""
        if self.state in ("running", "paused"):
            self.paused_since = None
            self._move("idle")


class run_field:
    """098 AG-02: ``agent.<name>`` forwards to ``agent.state.run_control.<field>``
    — a per-run counter lives in RunControl and ``begin_run`` clears it. Before
    the agent has a state (an ``Agent.__new__`` probe) the value sits on the
    instance."""

    def __init__(self, field: str):
        self.field = field

    def _rc(self, obj):
        return getattr(obj.__dict__.get("state"), "run_control", None)

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        rc = self._rc(obj)
        if rc is None:
            try:
                return obj.__dict__["_run_field_" + self.field]
            except KeyError:
                raise AttributeError(self.field) from None
        return getattr(rc, self.field)

    def __set__(self, obj, value) -> None:
        rc = self._rc(obj)
        if rc is None:
            obj.__dict__["_run_field_" + self.field] = value
        else:
            setattr(rc, self.field, value)


class owned:
    """Data descriptor: ``obj.<name>`` forwards to ``getattr(obj, holder).<field>``.

    The holder object is created with ``factory()`` on first access when the
    instance does not have one yet.
    """

    def __init__(self, holder: str, field: str, factory: Callable[[], Any]):
        self.holder, self.field, self.factory = holder, field, factory

    def _owner(self, obj):
        state = obj.__dict__.get(self.holder)
        if state is None:
            state = self.factory()
            obj.__dict__[self.holder] = state
        return state

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return getattr(self._owner(obj), self.field)

    def __set__(self, obj, value) -> None:
        setattr(self._owner(obj), self.field, value)
