"""095 W3: a third-party seat is not sent the schema of a verb it refuses.

A PUBLIC (room) session and a session created for a correspondent run the
session gates on every call (``core/security/policy_order.install_session_gates``).
A verb those gates refuse on EVERY call (``load_tool``, ``skill_manage``,
``propose_action``, ``owner_ask``...) used to ship its full schema anyway: about
2K tokens a call for a tool the model can only be refused (IN-12, X-25).

One predicate: the gate's own. Room = ``core.surfaces.room_policy.is_room_denied_call``
(it reads the ``room_denied`` rows plus the money and high-impact layers);
correspondent = ``correspondent_gate.is_high_impact_call`` (the
``correspondent_blocked`` rows plus its tool-id layers), less the scoped reply
tools. The decision is made once, at agent construction, by seat CLASS (the room
stamp, the correspondent-created stamp) — never by a live taint, so an owner DM
that a correspondent taints later keeps its schemas and the gate still refuses.
The schemas leave ``tools[]`` only (the Registry ``exclude_actions`` filter);
the actions stay registered, so a guessed call is refused by the gate with the
seat's own text.
"""
from typing import FrozenSet


def seat_refused_actions(controller, orchestrator) -> FrozenSet[str]:
    """Registered action names this seat's gate refuses on every call."""
    from core.surfaces.room_policy import is_public_session, is_room_denied_call
    public = is_public_session(orchestrator)
    correspondent = getattr(orchestrator, "_correspondent_session", False) is True
    if not (public or correspondent):
        return frozenset()
    # 101: a stamped admin session keeps every bundle schema. The owner can grant one
    # later, and the gate decides each call live.
    from core.surfaces.room_grants import is_bundle_call
    stamped = public and bool(getattr(orchestrator, "_room_admin", None))
    from agents.task.agent.core.correspondent_gate import _REPLY_TOOLS, is_high_impact_call
    actions = controller.registry.registry.actions
    out = set()
    for name, action in list(actions.items()):
        tool = getattr(action, "tool", None)
        if stamped and is_bundle_call(name, tool):
            continue
        if public and is_room_denied_call(name, tool):
            out.add(name)
        elif correspondent and name not in _REPLY_TOOLS and is_high_impact_call(name, tool):
            out.add(name)
    return frozenset(out)


def exclude_seat_refused_schemas(controller, orchestrator) -> FrozenSet[str]:
    """Drop the refused verbs from the exported schemas. Fail-open: the gate
    still refuses every call, so an error only costs tokens."""
    try:
        names = seat_refused_actions(controller, orchestrator)
        excluded = controller.exclude_actions
        excluded.extend(sorted(n for n in names if n not in excluded))
        return names
    except Exception:
        return frozenset()
