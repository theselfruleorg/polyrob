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

from dataclasses import dataclass
from typing import Any, Callable, List, Optional


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
