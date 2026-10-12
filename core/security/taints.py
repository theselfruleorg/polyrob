"""The three taints, ONE view (W5, owner decision D3).

A turn can hold three different "third parties are steering this" marks. They
keep their own scopes and their own stores (merging the stores would change
when each one clears, and the refusal taint must survive a restart):

========================  =========  =========================================
taint                     scope      store
========================  =========  =========================================
``correspondent``         session    ``orchestrator._correspondent_tainted``
                                     (+ the M02 sidecar, ``hitl_ingress.py``)
``read``                  turn       ``core.security.read_taint`` (orchestrator
                                     attribute + ``metadata["untrusted_read"]``)
``refusal``               run        ``core.security.refusal_taint`` (sqlite)
========================  =========  =========================================

Callers ask THIS module: :func:`of` gives the :class:`TaintSet`, the probes
answer one question each, and :func:`clear_on_owner_turn` is the ONE clear
point (``agents/task/agent/core/user_ingress.py`` calls it when the owner's own
message is drained into the session). Each taint is an owner security setting
or none: ``read_taint`` and ``refusal_taint`` are ``SECURITY_SETTINGS`` rows; the
correspondent taint is the chat access model's tier (a stranger's message is
never the owner).

Tier-0: imports only ``core``. Never raises; an unreadable marker reads tainted
where the store is durable (the refusal taint's own rule).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, FrozenSet, Optional

logger = logging.getLogger(__name__)

CORRESPONDENT = "correspondent"
READ = "read"
REFUSAL = "refusal"
ALL = (CORRESPONDENT, READ, REFUSAL)

#: The orchestrator attribute that holds the correspondent taint.
CORRESPONDENT_ATTR = "_correspondent_tainted"
#: The execution-context metadata key the step loop stamps it under.
CORRESPONDENT_META_KEY = "correspondent_tainted"


@dataclass(frozen=True)
class TaintSet:
    names: FrozenSet[str] = frozenset()

    def __contains__(self, name: object) -> bool:
        return name in self.names

    def __bool__(self) -> bool:
        return bool(self.names)

    @property
    def correspondent(self) -> bool:
        return CORRESPONDENT in self.names

    @property
    def read(self) -> bool:
        return READ in self.names

    @property
    def refusal(self) -> bool:
        return REFUSAL in self.names


EMPTY = TaintSet()


# -- correspondent (session) -------------------------------------------------

def correspondent_tainted(orchestrator: Any) -> bool:
    """The ONE reader of the correspondent taint on an orchestrator."""
    return getattr(orchestrator, CORRESPONDENT_ATTR, False) is True


def correspondent_probe(orchestrator: Any) -> Callable[[], bool]:
    """A zero-arg probe for an approval provider (read at call time, not now)."""
    return lambda: bool(getattr(orchestrator, CORRESPONDENT_ATTR, False))


# -- read (turn) -------------------------------------------------------------

def orchestrator_read_or_correspondent(orchestrator: Any) -> bool:
    """What the step loop stamps as ``metadata["untrusted_read"]``: the turn read
    third-party text, or a correspondent's message is in the session."""
    from core.security.read_taint import orchestrator_tainted
    return orchestrator_tainted(orchestrator) or correspondent_tainted(orchestrator)


def turn_tainted(execution_context: Any) -> bool:
    """This step's turn holds third-party text: the read taint (the owner's
    ``read_taint`` setting) or the correspondent taint (always). Per the step's
    context; a context with no metadata is untainted."""
    try:
        from core.security.read_taint import is_tainted
        if is_tainted(execution_context):
            return True
        meta = getattr(execution_context, "metadata", None)
        return bool(isinstance(meta, dict) and meta.get(CORRESPONDENT_META_KEY))
    except Exception:
        return True


def orchestrator_turn_tainted(orchestrator: Any) -> bool:
    """:func:`turn_tainted` for a gate that holds the orchestrator, not a context."""
    try:
        from core.security.read_taint import orchestrator_tainted
        return orchestrator_tainted(orchestrator) or correspondent_tainted(orchestrator)
    except Exception:
        return True


def cause(*, orchestrator: Any = None, execution_context: Any = None) -> Optional[str]:
    """The read/correspondent taint of this turn in words, for a gate's refusal
    (so the agent can tell the owner the truth), or None when untainted."""
    try:
        from core.security import read_taint
        tainted = (orchestrator is not None and read_taint.orchestrator_tainted(orchestrator)) \
            or (execution_context is not None and read_taint.is_tainted(execution_context))
        if tainted:
            src = read_taint.source(orchestrator=orchestrator,
                                    execution_context=execution_context)
            if src:
                return f"this run read an untrusted source: {src}"
        meta = getattr(execution_context, "metadata", None)
        if (orchestrator is not None and correspondent_tainted(orchestrator)) or \
                (isinstance(meta, dict) and meta.get(CORRESPONDENT_META_KEY)):
            return "a correspondent's message (not the owner's) is in this session"
        if tainted:
            return "this run read third-party content (web, mail, a file, a sub-agent)"
    except Exception:
        return "the taint state of this run could not be read"
    return None


# -- the set -----------------------------------------------------------------

def of(*, orchestrator: Any = None, execution_context: Any = None,
       session_id: Optional[str] = None) -> TaintSet:
    """Every taint this turn holds. Give what the caller has; a missing source
    contributes nothing (except a durable store that cannot be read)."""
    names = set()
    try:
        if orchestrator is not None and correspondent_tainted(orchestrator):
            names.add(CORRESPONDENT)
        meta = getattr(execution_context, "metadata", None)
        if isinstance(meta, dict) and meta.get(CORRESPONDENT_META_KEY):
            names.add(CORRESPONDENT)
        from core.security import read_taint
        # The context's ``untrusted_read`` stamp is read-OR-correspondent (the
        # step loop's legacy shape), so a stamped context reads as READ.
        if (orchestrator is not None and read_taint.orchestrator_tainted(orchestrator)) or \
                (execution_context is not None and read_taint.is_tainted(execution_context)):
            names.add(READ)
        sid = session_id or getattr(execution_context, "session_id", None) \
            or getattr(orchestrator, "session_id", None)
        if sid:
            from core.security import refusal_taint
            if refusal_taint.is_tainted(sid):
                names.add(REFUSAL)
    except Exception:
        logger.debug("taints.of: probe failed — reading tainted", exc_info=True)
        return TaintSet(frozenset(ALL))
    return TaintSet(frozenset(names))


def clear_on_owner_turn(orchestrator: Any) -> None:
    """The ONE clear point: the owner's own message entered the session, so the
    correspondent and read taints of the last turn and this run's refusal taint
    no longer speak for this one. Each clear is independent and fail-open."""
    if orchestrator is None:
        return
    try:
        orchestrator._clear_correspondent_taint()
    except Exception:
        pass
    try:
        from core.security.read_taint import clear
        clear(orchestrator)
    except Exception:
        pass
    try:
        from core.security.refusal_taint import clear
        clear(getattr(orchestrator, "session_id", "") or "")
    except Exception:
        pass


__all__ = ["CORRESPONDENT", "READ", "REFUSAL", "ALL", "CORRESPONDENT_ATTR",
           "CORRESPONDENT_META_KEY", "TaintSet", "EMPTY", "correspondent_tainted",
           "correspondent_probe", "orchestrator_read_or_correspondent", "turn_tainted",
           "orchestrator_turn_tainted", "cause", "of", "clear_on_owner_turn"]
