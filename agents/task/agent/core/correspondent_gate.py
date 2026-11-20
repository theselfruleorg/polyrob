"""WS-A capability gate — block high-impact tools while a session is "tainted" by
untrusted correspondent DATA (Fusion HIGH/Q9).

Correspondent replies enter as a user-role control message with prompt-level framing
(`<correspondent-message>` + `<untrusted_tool_result>`). That framing is a SOFT defense:
a strong injection could still try to drive a tool. This pre-tool-call hook is the
STRUCTURAL backstop — while the latest input on a session is correspondent-untrusted,
the dangerous tools (money, outbound messaging, code execution, delegation, browser)
are denied, so a forged email can never directly spend/send/execute. The owner clears
the taint simply by sending a genuine message (they are driving again).

Pure policy + a hook factory; the taint flag lives on the orchestrator (set on
correspondent injection, cleared on owner intake).

⚠️ **tool_id vs action-name (the load-bearing detail).** The pre-tool-call hook receives
the bare *action name* — ``run_code``, ``goal_create``, ``x402_fetch`` — never the
*tool_id* (``code_execution``, ``goal``, ``x402_pay``). A denylist keyed on tool_ids is
therefore dead: it only ever fires for a tool whose single action is literally named
after the tool_id. So the gate blocks in two layers (both in
``core/security/high_impact.py``):

  1. ``_HIGH_IMPACT_NAMES`` — explicit high-impact *action* names (delegation, skill/
     self-context, crypto trade verbs, git/github write verbs). Matched by name.
  2. ``HIGH_IMPACT_TOOL_IDS`` — whole tools whose every non-read action is high-impact,
     matched by RESOLVING the action's owning tool_id at hook time (via a resolver the
     wiring passes in, backed by ``Controller.get_action_details(name).tool``). This is
     the only way to cover dynamically-named actions such as MCP direct actions
     (``{server}_{tool}`` → tool_id ``mcp``).

Crypto tools (``hyperliquid``/``polymarket``) are deliberately EXCLUDED from
``HIGH_IMPACT_TOOL_IDS`` so their read verbs (``get_*``) stay allowed — a tainted
session may still answer "what's the price?"; their trade verbs are enumerated by name.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# The high-impact classification (both layers, the prefixes and the verb
# substrings) has one home, core/security/high_impact.py (proposal 125 S21f): the
# public-room gate reads the SAME policy. Import it from there.
from core.security.high_impact import is_high_impact_call


def build_tool_resolver(controller: Any) -> Callable[[str], Optional[str]]:
    """Return ``action_name -> owning tool_id`` backed by a Controller.

    Uses ``Controller.get_action_details(name).tool`` — the same registry seam the
    untrusted-wrap uses to resolve an action's source tool. Never raises: a missing
    controller, an unknown action, or a registry fault all degrade to ``None`` (the
    caller then falls back to the name-only decision).
    """
    def _resolve(action_name: str) -> Optional[str]:
        if controller is None:
            return None
        try:
            details = controller.get_action_details(action_name)
            return getattr(details, "tool", None) if details is not None else None
        except Exception as e:
            logger.debug("build_tool_resolver: get_action_details failed for %r: %s",
                         action_name, e)
            return None
    return _resolve


# ---------------------------------------------------------------------------
# D1 (2026-07-13 review): scoped reply-while-tainted. Every correspondent reply
# re-taints the session and taint blocks ALL outbound comms, so the agent could
# never answer the person who just wrote in — every round needed an owner turn.
# The exemption below permits message/send_email to EXACTLY the tainting
# (surface, address): 1:1, no cc/bcc, budget- and flag-gated.
# ---------------------------------------------------------------------------
# ⚠️ RUNTIME action names. `email_send` is EmailTool's decorated method (tool_id
# `email`, already self-prefixed so it is not double-namespaced). This set read
# `send_email` — the name of an UNDECORATED internal helper that is never registered
# as an action — so the exemption was dead for the email tool: a tainted session
# could never answer the very correspondent it was talking to via `email_send`, only
# via the generic `message(surface="email", ...)` action.
_REPLY_ACTIONS = frozenset({"message", "email_send"})
#: The tools that own a reply action (095 W2 seat probe).
_REPLY_TOOLS = frozenset({"message", "email"})


def _param(params: Any, key: str) -> Any:
    if params is None:
        return None
    if isinstance(params, dict):
        return params.get(key)
    return getattr(params, key, None)


def _reply_target(action_name: str, params: Any) -> tuple:
    """(surface, address) this call would message, or ('', '') when not a clean
    single-recipient reply shape (multi-recipient / cc / bcc are never exempt)."""
    name = str(action_name or "").strip().lower()
    if name == "message":
        # 0008: only a SEND is a reply. `delete`/`posts` act on the agent's own
        # posts in any chat — never something a correspondent's text may steer.
        if str(_param(params, "action") or "send").strip().lower() not in ("send", "reply"):
            return ("", "")
        # Like email_send's attachments: a file would carry workspace bytes to the
        # party that tainted the session — not a clean reply.
        if _param(params, "media_paths"):
            return ("", "")
        return (str(_param(params, "surface") or ""),
                str(_param(params, "target") or ""))
    if name == "email_send":
        # EmailSendAction has cc/bcc/attachments (2026-10-04): a cc or bcc widens
        # the blast radius, and an attachment would carry workspace files to the
        # party that tainted the session — none of them is a clean reply. A
        # comma-separated `to` never equals one tainting address, so it is not
        # exempt either.
        if _param(params, "cc") or _param(params, "bcc") or _param(params, "attachments"):
            return ("", "")
        to = _param(params, "to") or _param(params, "to_email")
        if isinstance(to, (list, tuple)):
            to = to[0] if len(to) == 1 else None
        return ("email", str(to or ""))
    return ("", "")


def _is_scoped_reply(action_name: str, params: Any, sources: Any) -> tuple:
    """(is_reply_to_tainting_party, surface, address)."""
    surface, address = _reply_target(action_name, params)
    if not surface or not address:
        return (False, surface, address)
    key = (surface, address.strip().lower())
    return (key in (sources or set()), surface, address.strip().lower())


_NO_STORE_WARNED = False


def build_reply_allowed(
    get_container: Callable[[], Any],
    get_user_id: Callable[[], str],
) -> Callable[[str, str], bool]:
    """Policy for the scoped tainted-reply exemption: flag + rounds budget.

    Denies unless ``CORRESPONDENT_REPLY_ENABLED`` (default OFF) AND the tenant has
    sent fewer than ``CORRESPONDENT_REPLY_MAX_ROUNDS`` outbound messages to that
    address in the last 24h (counted from the ConversationStore). Fail-CLOSED —
    if the budget can't be verified, the reply stays blocked (the owner may
    always unblock by replying).
    """
    def _allowed(surface: str, address: str) -> bool:
        try:
            from core.surfaces.config import SurfaceConfig
            if not SurfaceConfig.correspondent_reply_enabled():
                return False
            max_rounds = SurfaceConfig.correspondent_reply_max_rounds()
            container = get_container()
            store = (container.get_service("conversation_store")
                     if container else None)
            if store is None:
                # 125 S30 (D9): no store = the rounds budget cannot be checked —
                # deny (fail closed), with one warning per process.
                global _NO_STORE_WARNED
                if not _NO_STORE_WARNED:
                    _NO_STORE_WARNED = True
                    logger.warning("correspondent reply denied: no conversation_store "
                                   "to check the rounds budget")
                return False
            user_id = get_user_id() or ""
            return store.outbound_count_since(user_id, surface, address,
                                              86400) < max_rounds
        except Exception as e:  # fail-closed
            logger.debug("reply_allowed probe failed (deny): %s", e)
            return False
    return _allowed


def install_correspondent_gate(controller, orchestrator):
    """Enforce existing taint regardless of the flag admitting new correspondents."""
    hook = build_correspondent_gate(controller, orchestrator)
    controller.register_pre_tool_call_hook(hook, fail_mode="closed")
    return hook


def _is_owner_chat(orchestrator) -> bool:
    """The owner's own chat: not a correspondent's session and not a goal/cron run
    (an autonomous run gets no owner turn to clear a taint, and its workspace is the
    run's own). The chat history there is the owner's private conversation."""
    if getattr(orchestrator, "_correspondent_session", False) is True:
        return False
    from core.autonomy_marker import is_autonomous
    return not is_autonomous(getattr(orchestrator, "session_id", None))


def owner_chat_tainted(orchestrator) -> bool:
    """A correspondent's reply taints the owner's OWN chat right now (the H-MEM tail
    and the workspace row hide; foundation_layers.owner_chat_tainted)."""
    from core.security.taints import correspondent_tainted
    return correspondent_tainted(orchestrator) and _is_owner_chat(orchestrator)


def build_correspondent_gate(controller, orchestrator):
    """The correspondent gate over *orchestrator*'s taint (the policy chain,
    ``tools/controller/policy_chain.py``, registers it in its order)."""
    from core.security.taints import correspondent_probe
    hook = make_correspondent_gate_hook(
        correspondent_probe(orchestrator),
        resolve_tool=build_tool_resolver(controller),
        get_taint_sources=lambda: set(getattr(orchestrator, "_correspondent_taint_sources", None) or ()),
        reply_allowed=build_reply_allowed(
            lambda: getattr(orchestrator, "container", None),
            lambda: getattr(orchestrator, "user_id", "") or ""),
        owner_chat=lambda: _is_owner_chat(orchestrator),
    )

    def _seat_denies(tool_id: str) -> Optional[str]:
        """095 W2: "correspondent" on a correspondent's session for a high-impact
        tool (the reply tool excepted: the scoped reply may use it)."""
        if not getattr(orchestrator, "_correspondent_session", False) is True:
            return None
        if str(tool_id or "") in _REPLY_TOOLS:
            return None
        return "correspondent" if is_high_impact_call(tool_id, tool_id) else None
    hook.seat_denies = _seat_denies
    return hook


def make_correspondent_gate_hook(
    get_tainted: Callable[[], bool],
    resolve_tool: Optional[Callable[[str], Optional[str]]] = None,
    get_taint_sources: Optional[Callable[[], Any]] = None,
    reply_allowed: Optional[Callable[[str, str], bool]] = None,
    owner_chat: Optional[Callable[[], bool]] = None,
):
    """Pre-tool-call hook ``(action_name, params, context) -> Optional[str]``.

    Returns a denial reason (string) for a high-impact tool while the session is tainted
    by untrusted correspondent data; None otherwise. Fail-CLOSED: if the taint probe
    raises, a high-impact tool is denied (we can't prove the session is clean).

    ``resolve_tool`` maps an action name to its owning tool_id so a tool_id-level
    denylist (:data:`HIGH_IMPACT_TOOL_IDS`) actually fires — the pre-hook only ever sees
    the bare action name. It is called defensively: a resolver fault degrades to the
    name-only decision (never raises out of the hook, never silently opens a hole for a
    name-level high-impact action).

    ``get_taint_sources`` + ``reply_allowed`` enable the D1 scoped-reply exemption:
    while tainted, ``message``/``send_email`` to EXACTLY a tainting (surface, address)
    is permitted when ``reply_allowed(surface, address)`` approves (flag + rounds
    budget). Everything else stays denied.

    ``owner_chat`` (True = an owner chat, not a correspondent's own session) adds the
    ``owner_workspace`` tools (``core.tool_capabilities``) to the denied set: a
    correspondent's reply that taints an OWNER chat must not read the owner's files
    for the scoped reply to carry out. Fail-closed: a probe error counts as an owner chat.
    """
    def _hook(action_name: str, params: Any, context: Any) -> Optional[str]:
        tool_id: Optional[str] = None
        resolve_failed = False
        if resolve_tool is not None:
            try:
                tool_id = resolve_tool(action_name)
            except Exception as e:  # resolution must never break the gate
                logger.debug("correspondent gate tool resolve failed: %s", e)
                tool_id = None
                resolve_failed = True
        blocked = is_high_impact_call(action_name, tool_id)
        if not blocked and owner_chat is not None and (tool_id is not None or resolve_failed):
            from core.tool_capabilities import ids_with
            # Fail-CLOSED: an action whose tool cannot be resolved may be a file act.
            if resolve_failed or str(tool_id).strip().lower() in ids_with("owner_workspace"):
                try:
                    blocked = bool(owner_chat())
                except Exception:
                    blocked = True
        if not blocked:
            return None
        try:
            tainted = bool(get_tainted())
        except Exception as e:  # fail-closed: can't prove clean -> deny the dangerous tool
            logger.debug("correspondent gate taint probe failed (deny): %s", e)
            tainted = True
        if tainted:
            # D1 scoped-reply exemption (never widens beyond the tainting party).
            if (get_taint_sources is not None and reply_allowed is not None
                    and str(action_name or "").strip().lower() in _REPLY_ACTIONS):
                try:
                    is_reply, surface, address = _is_scoped_reply(
                        action_name, params, get_taint_sources())
                    if is_reply and reply_allowed(surface, address):
                        logger.info(
                            "correspondent gate: scoped tainted reply to %s:%s "
                            "permitted", surface, address)
                        return None
                except Exception as e:  # fail-closed: exemption probe never opens
                    logger.debug("scoped-reply exemption probe failed (deny): %s", e)
            from core.security.refusals import record_refusal
            from core.tool_gates import refusal_text
            record_refusal("correspondent_taint", tool=action_name,
                           user_id=getattr(context, "user_id", None) or "",
                           session_id=getattr(context, "session_id", None) or "",
                           kind="seat:correspondent")
            return refusal_text("seat:correspondent", action=action_name)
        return None
    return _hook

