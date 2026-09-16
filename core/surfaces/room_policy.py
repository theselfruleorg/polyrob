"""044: what a PUBLIC (room) session may carry and may do.

A session bound to a group/supergroup/channel is PUBLIC: its audience is many
humans, so it carries none of the owner tenant's private state and runs with a
read-only room toolset. Pure helpers.

The orchestrator flag is stamped in THREE places, all from the one derivation
:func:`is_public_source` (044 C1/C2):
  * ``TaskAgent.create_session`` — from the session SOURCE, before the chat bind,
    so the flag never depends on ``SINGULAR_CHAT_ENABLED``;
  * ``binding.py::bind_chat_surface`` — a re-affirmation that can only RAISE it;
  * ``_recreate_orchestrator`` — restored from the session's durable metadata,
    because the recreate path has no surface bus to ask when the bus is off.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def is_public_session(orchestrator: Any) -> bool:
    """True when the bound chat is a room. Unbound / unknown -> False (private)."""
    return bool(getattr(orchestrator, "_public_session", False))


def is_public_source(session_source: Any) -> bool:
    """True when a ``SessionSource`` names a ROOM (any non-DM chat type).

    The ONE derivation every stamp site shares (044 C2). It used to live only
    inside ``binding.py::bind_chat_surface``, which returns EARLY when the
    Singular Chat bus is off — so with ``SINGULAR_CHAT_ENABLED`` unset (the
    default) a room session was built with ``_public_session`` never assigned,
    i.e. private-shaped: owner docs, tenant recall, the episodic digest and an
    ungated toolset, in a public room. ``create_session`` now stamps from here
    BEFORE binding, and the recreate path restores the stamp from the session's
    own durable metadata, so the flag can never depend on a transport flag.
    """
    if session_source is None:
        return False
    return (getattr(session_source, "chat_type", "dm") or "dm") != "dm"


#: Read-only room toolset. web_fetch results are untrusted-wrapped already;
#: defi_data's portfolio/positions/reconcile verbs are denied by name below.
#:
#: ⚠️ ``knowledge`` was here and is NOT (044 C3): its actions carry no capability
#: bits, so `kb_ingest`/`kb_remove` were reachable from a room — a member could
#: read, POISON and DELETE the owner's knowledge base (`kb_remove` with no
#: source clears the collection). The whole `kb_*` family is now denied by
#: prefix below as well, so re-adding the tool via ``GROUP_TURN_TOOLS`` still
#: cannot reach a write.
DEFAULT_ROOM_TOOLS = ("task", "web_fetch", "defi_data")

#: Action names a room turn may never call, whoever spoke. Groups: deferred
#: execution (a later owner-tenant run would execute what a stranger planted),
#: cross-session recall (owner state read back into a public reply), outbound to
#: other targets, money-adjacent reads, self-modification, control.
ROOM_DENIED_ACTIONS = frozenset({
    # deferred execution
    "goal_create", "goal_cancel", "cronjob_schedule", "cronjob_cancel",
    "skill_manage", "self_context_manage", "preferences", "owner_doc_manage",
    "load_tool", "tool_manage_install", "mcp_install", "self_modify",
    # recall / owner state
    "session_search", "memory_search", "memory", "contact_history",
    "recent_activity", "agent_status", "insights", "usage_summary",
    # the owner's knowledge base: read AND write (044 C3). `kb_*` is also
    # denied by PREFIX below, so a future verb cannot be forgotten here.
    "kb_search", "kb_ingest", "kb_list", "kb_remove",
    # outbound to anywhere but this room
    "message", "send_email", "email_send",
    # money-adjacent
    "x402_invoice_x402_request", "x402_invoice_accounting", "x402_invoice_x402_invoices",
    "defi_data_portfolio", "defi_data_positions", "defi_data_reconcile",
    "defi_data_balances",
    # delegation / control
    "delegate_task", "subtask", "parallel_subtasks", "autonomy_control",
})


def _forbidden_tool_ids() -> frozenset:
    """Tool ids a room session may never load, even via ``GROUP_TURN_TOOLS``: any
    money/exec/delegate-blocked/high-impact tool_id, EXCEPT ``web_fetch`` — the
    room's designated (already untrusted-wrapped) read tool, the same exception
    :func:`is_room_denied_call` carves for it at the action-name level."""
    from core.tool_capabilities import ids_with
    forbidden = (ids_with("money") | ids_with("exec") | ids_with("delegate_blocked")
                 | ids_with("high_impact"))
    return forbidden - {"web_fetch"}


def room_tool_ids() -> list:
    """The room toolset: env ``GROUP_TURN_TOOLS`` (comma list) else the default.
    A money/exec/delegate-blocked id in the env is DROPPED with a WARN — the env
    can narrow the room toolset, never widen it past the audience bound."""
    raw = os.getenv("GROUP_TURN_TOOLS")
    ids = ([t.strip() for t in raw.split(",") if t.strip()] if raw is not None
           else list(DEFAULT_ROOM_TOOLS))
    forbidden = _forbidden_tool_ids()
    kept = []
    for t in ids:
        if t in forbidden:
            logger.warning("GROUP_TURN_TOOLS: dropping %r — money/exec/delegate-blocked "
                           "tools are never room tools", t)
            continue
        kept.append(t)
    # Fix round 1 (review): an empty result is honest — NEVER floored back to
    # DEFAULT_ROOM_TOOLS (that would silently defeat an operator's deliberate
    # `GROUP_TURN_TOOLS=""` lockdown). Loud instead of silent: a room whose toolset
    # ends up empty can only chat (no read tool either) until the operator notices.
    if not kept:
        logger.warning("room toolset is empty: GROUP_TURN_TOOLS=%r — the room agent "
                       "can only chat", raw)
    return kept


def _is_money_call(name: str, tool_id: Optional[str]) -> bool:
    """Does this call belong to a tool the capability table marks ``money``?

    Resolved by owning tool_id when the caller could resolve one, else by the
    action-name NAMESPACE (``tools/controller/tool_management.py`` registers every
    action as ``{tool_id}_{action}``), so the decision survives a controller that
    cannot answer.

    ⚠️ The namespace fallback matches the LONGEST tool id the name starts with,
    not any money id: ``polymarket_data`` (read-only market data) and
    ``polymarket`` (a money tool) share a prefix, so a plain
    ``startswith("polymarket_")`` would deny every read-only quote as a money
    verb. Fail-CLOSED: an unreadable capability table denies rather than allows.
    """
    try:
        from core.tool_capabilities import TOOL_CAPABILITIES, ids_with
        money = ids_with("money")
    except Exception as e:  # pragma: no cover - the table is a static dict
        logger.warning("room gate: capability table unreadable (denying): %s", e)
        return True
    if tool_id:
        return str(tool_id).strip().lower() in money
    owner = ""
    for t in TOOL_CAPABILITIES:
        if (name == t or name.startswith(f"{t}_")) and len(t) > len(owner):
            owner = t
    return bool(owner) and owner in money


def is_room_denied_call(action_name: Optional[str], tool_id: Optional[str]) -> bool:
    name = str(action_name or "").strip().lower()
    if not name:
        return True  # fail-closed on a nameless call
    if name in ROOM_DENIED_ACTIONS:
        return True
    # 044 C3: the owner's knowledge base, by PREFIX — the `knowledge` tool's
    # actions carry no capability bits, so nothing below would catch a verb
    # added to it later. A room never reads and never writes the owner's KB.
    if name.startswith("kb_") or tool_id == "knowledge":
        return True
    # 044 spec ratchet 4: NO money verb, ever, whoever spoke. The high-impact
    # check below is not a superset — `hyperliquid` and `polymarket` carry
    # `money` but NOT `high_impact`, so `hyperliquid_place_order` passed the gate
    # (it can only ever be reached if a schema slips past the toolset bound,
    # which is exactly the case this second line of defence exists for).
    if _is_money_call(name, tool_id):
        return True
    from agents.task.agent.core.correspondent_gate import is_high_impact_call
    if is_high_impact_call(name, tool_id):
        # web_fetch is high-impact by capability table but is the room's read tool.
        return not (tool_id == "web_fetch" or name.startswith("web_fetch"))
    return False


def make_room_gate_hook(get_public: Callable[[], bool],
                        resolve_tool: Optional[Callable[[str], Optional[str]]] = None):
    """Pre-tool-call hook ``(action_name, params, context) -> Optional[str]``.
    Denies every room-denied call while the session is PUBLIC. Fail-CLOSED: if
    the public probe raises, the call is denied."""
    def _hook(action_name: str, params, context) -> Optional[str]:
        try:
            public = bool(get_public())
        except Exception as e:
            logger.debug("room gate probe failed (deny): %s", e)
            public = True
        if not public:
            return None
        tool_id = None
        if resolve_tool is not None:
            try:
                tool_id = resolve_tool(action_name)
            except Exception:
                tool_id = None
        if is_room_denied_call(action_name, tool_id):
            return (f"'{action_name}' is not available in a group chat. This session "
                    "speaks into a public room and can only read and reply. The owner "
                    "can do this from their private chat with me.")
        return None
    return _hook
