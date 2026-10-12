"""The ONE definition of "who is driving this turn" (W5, owner decision D3).

Before W5 five modules answered the question in their own words: this module's
``owner_turn_refusal``, ``tools/controller/turn_origin.py``
(``_is_forged_or_autonomous_turn`` / ``_is_autonomous_goal_turn``),
``tools/goal_tools.py::owner_seat_turn``, ``core/config_policy/compute_posture.py``
and the X pack's own copies. They disagreed at the edges: a seat context with
``role="owner"`` was a genuine turn to the money gates and "a leaf agent" here.
Every one of them now reads the predicates below; the others delegate.

The building blocks, each one rule:

- :func:`is_top_role` — the context is the main agent (``orchestrator``; the
  legacy seat role ``owner`` is the same shape). An unset role is a leaf
  (least privilege, H7).
- :func:`is_leaf` — a delegated sub-agent or a leaf context.
- :func:`forged_kind` — a self-wake / delegation-result re-entry (SK-F10).
- :func:`is_room_turn` — a room-bound (group) turn (044 T5).
- :func:`is_owner_tenant` — the principal is the owner (``is_owner_local_safe``).

and the compositions the gates use:

- :func:`owner_turn_refusal` — the three clauses a "put something in the world"
  verb states (owner tenant, not a leaf, not forged). An autonomous run of the
  owner tenant passes (WS-8: those runs hold the compute toolset).
- :func:`compute_turn_allows` — the same clauses, for the compute posture gate.
- :func:`is_forged_or_autonomous` — anything other than the owner typing now.
- :func:`is_autonomous_goal_turn` — exactly the unattended goal/cron MAIN run.
- :func:`is_genuine_owner_turn` — the owner typing now, on the owner tenant.

Fail-CLOSED: any fault in resolution refuses / answers the least-privileged way.
Tier-0: imports only ``core``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Execution-context roles that are the main agent. ``owner`` is the legacy role
#: the owner seats used before W5 (``core.security.turn_facts.owner_seat_context``
#: is the one seat constructor now); it is the same shape, never a leaf.
TOP_ROLES = ("orchestrator", "owner")

#: The room-bound turn kind (``step_execution`` stamps it for a public session).
ROOM_TURN_KIND = "group"


def _metadata(execution_context: Any) -> dict:
    meta = getattr(execution_context, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def is_top_role(execution_context: Any) -> bool:
    return getattr(execution_context, "role", "leaf") in TOP_ROLES


def is_leaf(execution_context: Any, controller_self: Any = None) -> bool:
    """A delegated sub-agent / leaf context (or one whose role is not the main agent)."""
    if bool(getattr(execution_context, "is_sub_agent", False)
            or getattr(controller_self, "_is_sub_agent", False)):
        return True
    return not is_top_role(execution_context)


def forged_kind(execution_context: Any) -> Optional[str]:
    """The forged re-entry kind stamped on this turn, or None."""
    from core.security.forged_turns import FORGED_TURN_KINDS
    kind = _metadata(execution_context).get("turn_kind")
    return kind if kind in FORGED_TURN_KINDS else None


def is_room_turn(execution_context: Any) -> bool:
    return _metadata(execution_context).get("turn_kind") == ROOM_TURN_KIND


def _session_id(execution_context: Any, controller_self: Any = None) -> str:
    return (getattr(execution_context, "session_id", None)
            or getattr(controller_self, "session_id", "") or "")


def is_autonomous_session(execution_context: Any, controller_self: Any = None) -> bool:
    """The session is a goal/cron/planner run (``core.autonomy_marker``). Raises on a probe fault."""
    from core.autonomy_marker import is_autonomous
    return bool(is_autonomous(_session_id(execution_context, controller_self)))


def is_owner_tenant(execution_context: Any) -> bool:
    from core.config_policy import local_mode_enabled
    from core.instance import is_owner_local_safe, resolve_owner_principal
    return is_owner_local_safe(getattr(execution_context, "user_id", None),
                               owner_principal=resolve_owner_principal(),
                               local_enabled=local_mode_enabled())


def is_forged_or_autonomous(execution_context: Any, controller_self: Any = None) -> bool:
    """True for a turn that must never auto-activate or promote its own content:
    a leaf / sub-agent, a forged re-entry, a room turn, or an autonomous run.
    Fail CLOSED: a probe fault answers True."""
    if is_leaf(execution_context, controller_self):
        return True
    if forged_kind(execution_context) or is_room_turn(execution_context):
        return True
    try:
        return is_autonomous_session(execution_context, controller_self)
    except Exception:
        logger.debug("is_forged_or_autonomous: autonomy-marker probe raised — "
                     "treating as forged (fail-closed)", exc_info=True)
        return True


def is_autonomous_goal_turn(execution_context: Any, controller_self: Any = None) -> bool:
    """True ONLY for a goal/cron-dispatched turn on the MAIN agent (stricter than
    :func:`is_forged_or_autonomous`). Every uncertainty answers False."""
    if is_leaf(execution_context, controller_self):
        return False
    if forged_kind(execution_context):
        return False
    try:
        return is_autonomous_session(execution_context, controller_self)
    except Exception:
        logger.debug("is_autonomous_goal_turn: autonomy-marker probe raised — "
                     "answering False (fail-closed)", exc_info=True)
        return False


def owner_turn_refusal(execution_context: Any, *, verb: str, does: str,
                       public: str) -> Optional[str]:
    """Non-None -> refuse, with this message.

    *does* completes "a delegated sub-agent never …" (e.g. ``"chooses what
    the world sees"``); *public* completes "only the owner tenant may …"
    (e.g. ``"put something at a public URL"``).
    """
    if execution_context is None:
        return f"{verb} requires an execution context"
    try:
        if getattr(execution_context, "is_sub_agent", False):
            return f"{verb} denied: a delegated sub-agent never {does}"
        if not is_top_role(execution_context):
            return f"{verb} denied: a leaf agent never {does}"
        if forged_kind(execution_context):
            return (f"{verb} denied: a self-wake / delegation-result re-entry is not "
                    f"an owner asking to ship")
        if not is_owner_tenant(execution_context):
            return f"{verb} denied: only the owner tenant may {public}"
    except Exception:
        return f"{verb} denied: capability gate unavailable"
    return None


def compute_turn_allows(execution_context: Any) -> bool:
    """The turn half of ``compute_posture_allows``: the clauses of
    :func:`owner_turn_refusal` (an autonomous owner-tenant run passes)."""
    return owner_turn_refusal(execution_context, verb="compute", does="holds host compute",
                              public="hold host compute") is None


def is_leaf_or_forged(execution_context: Any) -> bool:
    """No context, a leaf / sub-agent, or a forged re-entry: never lets the
    turn drive an owner-held account (the X browser rail). Fail closed."""
    if execution_context is None:
        return True
    try:
        return is_leaf(execution_context) or bool(forged_kind(execution_context))
    except Exception:
        return True


def is_genuine_owner_turn(execution_context: Any, controller_self: Any = None) -> bool:
    """The owner typing now: not forged/autonomous/room/leaf, and the owner tenant.
    Fail closed. (``None`` is not a turn — callers decide what a direct call means.)"""
    if execution_context is None:
        return False
    try:
        if is_forged_or_autonomous(execution_context, controller_self):
            return False
        return is_owner_tenant(execution_context)
    except Exception:
        return False


__all__ = ["TOP_ROLES", "ROOM_TURN_KIND", "is_top_role", "is_leaf", "forged_kind",
           "is_room_turn", "is_autonomous_session", "is_owner_tenant",
           "is_forged_or_autonomous", "is_autonomous_goal_turn", "owner_turn_refusal",
           "compute_turn_allows", "is_leaf_or_forged", "is_genuine_owner_turn"]
