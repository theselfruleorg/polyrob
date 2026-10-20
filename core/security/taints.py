"""The taints, ONE view (W5, owner decision D3).

========================  =========  =========================================
taint                     scope      store
========================  =========  =========================================
``correspondent``         session    ``orchestrator._correspondent_tainted``
                                     (+ the M02 sidecar, ``hitl_ingress.py``)
========================  =========  =========================================

Callers ask THIS module: the probes answer one question each, and
:func:`clear_on_owner_turn` is the ONE clear point
(``agents/task/agent/core/user_ingress.py`` calls it when the owner's own
message is drained into the session). The correspondent taint is the chat
access model's tier (a stranger's message is never the owner).

Tier-0: imports only ``core``. Never raises.
"""
from __future__ import annotations

from typing import Any, Callable

#: The orchestrator attribute that holds the correspondent taint.
CORRESPONDENT_ATTR = "_correspondent_tainted"
#: The execution-context metadata key the step loop stamps it under.
CORRESPONDENT_META_KEY = "correspondent_tainted"


# -- correspondent (session) -------------------------------------------------

def correspondent_tainted(orchestrator: Any) -> bool:
    """The ONE reader of the correspondent taint on an orchestrator."""
    return getattr(orchestrator, CORRESPONDENT_ATTR, False) is True


def correspondent_probe(orchestrator: Any) -> Callable[[], bool]:
    """A zero-arg probe for an approval provider (read at call time, not now)."""
    return lambda: bool(getattr(orchestrator, CORRESPONDENT_ATTR, False))


def turn_tainted(execution_context: Any) -> bool:
    """A correspondent's message is in this step's session (per the step's
    context; a context with no metadata is untainted)."""
    meta = getattr(execution_context, "metadata", None)
    return bool(isinstance(meta, dict) and meta.get(CORRESPONDENT_META_KEY))


def clear_on_owner_turn(orchestrator: Any) -> None:
    """The ONE clear point: the owner's own message entered the session, so the
    correspondent taint no longer speaks for this one. Fail-open."""
    if orchestrator is None:
        return
    try:
        orchestrator._clear_correspondent_taint()
    except Exception:
        pass


__all__ = ["CORRESPONDENT_ATTR", "CORRESPONDENT_META_KEY", "correspondent_tainted",
           "correspondent_probe", "turn_tainted", "clear_on_owner_turn"]
