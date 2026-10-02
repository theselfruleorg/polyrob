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

from core.lazy_views import lazy_module_getattr, view
from core.verb_policy import ids_where

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
#: other targets, money-adjacent reads, self-modification, control. The owner's
#: knowledge base (``kb_*``, read AND write, 044 C3) is also denied by PREFIX
#: below, so a future verb cannot be forgotten here.
#: 067 P1: a DERIVED view of the per-action policy table (core/verb_policy.py;
#: rows in core/verb_policy_rows.py) — deny a verb there with ``room_denied``.
#: 067 P4 prerequisite: LAZY (``core/lazy_views.py``), built on first read after
#: the pack loader's phase 1 (the module ``__getattr__`` at the end of this file).
def _room_denied_actions():
    return ids_where(room_denied=True)


#: D60: tool ids a room session may NEVER load, named explicitly, whatever the
#: capability table says about them today.
#:
#: ⚠️ The capability derivation below is necessary and was NOT sufficient. The
#: docstring claimed the env "can narrow the room toolset, never widen it", and
#: ``GROUP_TURN_TOOLS=filesystem`` walked straight through it: ``filesystem``
#: carries none of ``money``/``exec``/``delegate_blocked``/``high_impact``, so a
#: room full of strangers got read and write access to the owner tenant's
#: workspace. These are the tools whose AUDIENCE, not whose capability bit,
#: rules them out: the host, the owner's files, his browser session, his mail,
#: his MCP credentials, his deployed apps.
ROOM_FORBIDDEN_TOOL_IDS = frozenset({
    # the host and the owner's files
    "filesystem", "shell", "process", "coding", "code_execution", "git",
    "self_env", "app_service",
    # the owner's own sessions, credentials and identity on other services
    "browser", "mcp", "email", "x_browser", "dapp_browser",
})

#: The ONLY ids a room turn may be given. A closed ALLOWLIST, because every
#: deny-list in this file has already been found short once: a tool added to
#: the tree next month is refused here by default and must be NAMED here to be
#: reachable from a public room. Today that is exactly
#: :data:`DEFAULT_ROOM_TOOLS`, so ``GROUP_TURN_TOOLS`` can only narrow it —
#: which is what the env was always documented to do.
ROOM_ALLOWED_TOOL_IDS = frozenset(DEFAULT_ROOM_TOOLS)


def _forbidden_tool_ids() -> frozenset:
    """Tool ids a room session may never load, even via ``GROUP_TURN_TOOLS``: any
    money/exec/delegate-blocked/high-impact tool_id, plus the explicit
    :data:`ROOM_FORBIDDEN_TOOL_IDS`, EXCEPT ``web_fetch`` — the room's
    designated (already untrusted-wrapped) read tool, the same exception
    :func:`is_room_denied_call` carves for it at the action-name level.

    Fail-CLOSED: an unreadable capability table still forbids the explicit set.
    """
    try:
        from core.tool_capabilities import ids_with
        forbidden = (ids_with("money") | ids_with("exec")
                     | ids_with("delegate_blocked") | ids_with("high_impact"))
    except Exception as e:  # pragma: no cover - the table is a static dict
        logger.warning("room toolset: capability table unreadable (%s) — the "
                       "explicit forbidden set still applies", e)
        forbidden = frozenset()
    return (frozenset(forbidden) | ROOM_FORBIDDEN_TOOL_IDS) - {"web_fetch"}


def room_tool_ids() -> list:
    """The room toolset: env ``GROUP_TURN_TOOLS`` (comma list) else the default.

    Two gates, in this order, and the env can only ever NARROW:

    1. a money/exec/delegate-blocked/high-impact id, or one of the explicitly
       audience-forbidden :data:`ROOM_FORBIDDEN_TOOL_IDS`, is DROPPED;
    2. anything outside the closed :data:`ROOM_ALLOWED_TOOL_IDS` is DROPPED.

    Both drops are WARNed by name — an operator who narrowed a room toolset by
    typo must be able to see which id went and why.
    """
    raw = os.getenv("GROUP_TURN_TOOLS")
    ids = ([t.strip() for t in raw.split(",") if t.strip()] if raw is not None
           else list(DEFAULT_ROOM_TOOLS))
    forbidden = _forbidden_tool_ids()
    kept = []
    for t in ids:
        if t in forbidden:
            logger.warning("GROUP_TURN_TOOLS: dropping %r — money/exec/host/"
                           "owner-state tools are never room tools", t)
            continue
        if t not in ROOM_ALLOWED_TOOL_IDS:
            logger.warning("GROUP_TURN_TOOLS: dropping %r — a room turn may only "
                           "load %s; a tool must be named room-safe to be "
                           "reachable from a public room",
                           t, ", ".join(sorted(ROOM_ALLOWED_TOOL_IDS)))
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

    Resolved by owning tool_id when the caller could resolve one (an EXACT
    tool id, lower-cased), else by the ONE action predicate
    ``core.money.classify.money_action`` (verb-policy row, then the LONGEST
    tool-id namespace — so ``polymarket_data`` reads are not money), so the
    decision survives a controller that cannot answer. Fail-CLOSED: an
    unreadable classification denies rather than allows.
    """
    try:
        from core.money.classify import money_action, money_tool_ids
        if tool_id:
            return str(tool_id).strip().lower() in money_tool_ids()
        return bool(money_action(name))
    except Exception as e:
        logger.warning("room gate: capability table unreadable (denying): %s", e)
        return True


def is_room_denied_call(action_name: Optional[str], tool_id: Optional[str]) -> bool:
    name = str(action_name or "").strip().lower()
    if not name:
        return True  # fail-closed on a nameless call
    if name in view(__name__, "ROOM_DENIED_ACTIONS"):
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


# 067 P4 prerequisite: ROOM_DENIED_ACTIONS, built on first read.
__getattr__ = lazy_module_getattr(__name__, {"ROOM_DENIED_ACTIONS": _room_denied_actions})
