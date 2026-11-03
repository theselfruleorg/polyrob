"""One transition table per machine (098 S1).

A machine names its states, the allowed edges and the terminal states. Callers
ask :meth:`StateMachine.check` before a write; the guarded write itself lives in
:mod:`core.state.sql`. A forbidden edge is refused and emits
``state_illegal_transition``; in strict mode (on in tests) it
raises :class:`IllegalTransition` instead, so a test finds it before deploy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

_STRICT = False


def strict_mode() -> bool:
    """Strict: an illegal edge raises instead of refusing. Off in production,
    on in the test suite (``tests/conftest.py`` calls :func:`set_strict`) — a
    test switch, not an operator flag."""
    return _STRICT


def set_strict(on: bool) -> None:
    global _STRICT
    _STRICT = bool(on)


class IllegalTransition(Exception):
    """An edge the machine's table forbids (raised only in strict mode)."""

    def __init__(self, machine: str, frm: str, to: str):
        super().__init__(f"{machine}: illegal transition {frm!r} -> {to!r}")
        self.machine, self.frm, self.to = machine, frm, to


#: name -> machine. Every StateMachine registers itself on construction.
REGISTRY: Dict[str, "StateMachine"] = {}


@dataclass(frozen=True)
class StateMachine:
    name: str
    states: FrozenSet[str]
    transitions: Mapping[str, FrozenSet[str]]
    terminal: FrozenSet[str] = frozenset()
    #: legacy stored value -> state (e.g. "error" -> "failed")
    aliases: Mapping[str, str] = field(default_factory=dict)
    #: states a fresh row may start in (reachability roots); default: every
    #: state with no incoming edge, else the first listed state.
    initial: FrozenSet[str] = frozenset()

    def __post_init__(self):
        states = frozenset(self.states)
        trans = {str(k): frozenset(v) for k, v in dict(self.transitions).items()}
        object.__setattr__(self, "states", states)
        object.__setattr__(self, "transitions", trans)
        object.__setattr__(self, "terminal", frozenset(self.terminal))
        object.__setattr__(self, "initial", frozenset(self.initial))
        object.__setattr__(self, "aliases", dict(self.aliases))
        for frm, tos in trans.items():
            unknown = ({frm} | set(tos)) - states
            if unknown:
                raise ValueError(f"{self.name}: unknown state(s) {sorted(unknown)}")
        for t in self.terminal:
            if trans.get(t):
                raise ValueError(f"{self.name}: terminal state {t!r} has outgoing edges")
        if set(self.aliases.values()) - states:
            raise ValueError(f"{self.name}: alias target not a state")
        REGISTRY[self.name] = self

    # -- queries -------------------------------------------------------------

    def normalize(self, state: Optional[str]) -> str:
        s = "" if state is None else str(state)
        return self.aliases.get(s, s)

    def allowed(self, frm: str, to: str) -> bool:
        return self.normalize(to) in self.transitions.get(self.normalize(frm), frozenset())

    def sources_of(self, to: str) -> FrozenSet[str]:
        """Every from-state with an edge to ``to``."""
        to = self.normalize(to)
        return frozenset(f for f, tos in self.transitions.items() if to in tos)

    def check(self, frm: str, to: str, *, ctx: Optional[Mapping[str, Any]] = None) -> bool:
        """True when ``frm -> to`` is an edge. Otherwise emit
        ``state_illegal_transition`` and return False (raise in strict mode)."""
        if self.allowed(frm, to):
            return True
        _emit_illegal(self.name, self.normalize(frm), self.normalize(to), ctx)
        if strict_mode():
            raise IllegalTransition(self.name, frm, to)
        return False

    def require_sources(self, to: str, from_states: Optional[Iterable[str]],
                        *, ctx: Optional[Mapping[str, Any]] = None) -> tuple:
        """The from-states for a guarded write to ``to``: ``None`` means every
        source in the table; an explicit list is checked edge by edge, and the
        forbidden ones are dropped (or raise in strict mode)."""
        if from_states is None:
            return tuple(sorted(self.sources_of(to)))
        out = []
        for f in from_states:
            if self.check(f, to, ctx=ctx):
                out.append(self.normalize(f))
        return tuple(out)

    def roots(self) -> FrozenSet[str]:
        if self.initial:
            return self.initial
        incoming = {t for tos in self.transitions.values() for t in tos}
        roots = frozenset(self.states - incoming)
        return roots

    def validate(self) -> list:
        """Structural problems (S1): unreachable states and non-terminal dead ends."""
        problems = []
        seen, todo = set(self.roots()), list(self.roots())
        while todo:
            for nxt in self.transitions.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        if self.roots():
            for s in sorted(self.states - seen):
                problems.append(f"{self.name}: {s!r} is unreachable")
        for s in sorted(self.states - self.terminal):
            if not self.transitions.get(s):
                problems.append(f"{self.name}: non-terminal {s!r} has no exit")
        return problems


#: The event sink (``core.event_log`` registers itself on import). Kept as a
#: hook so this tier-0 module never imports ``core.event_log`` — that import
#: pulled every machine-defining module into core's large import cycle.
_EVENT_SINK = None


def set_event_sink(fn) -> None:
    """``fn(kind, attrs)`` records an event. ``None`` disables recording."""
    global _EVENT_SINK
    _EVENT_SINK = fn


def _emit_illegal(machine: str, frm: str, to: str, ctx: Optional[Mapping[str, Any]]) -> None:
    logger.warning("state_illegal_transition %s: %s -> %s %s", machine, frm, to, dict(ctx or {}))
    sink = _EVENT_SINK
    if sink is None:
        return
    try:
        sink("state_illegal_transition",
             {"machine": machine, "from": frm, "to": to,
              **{k: str(v)[:200] for k, v in dict(ctx or {}).items()}})
    except Exception:
        pass
