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
after the tool_id. So this module blocks in two layers:

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

# ---------------------------------------------------------------------------
# Layer 1 — high-impact *action* names (matched against the bare action name the
# pre-hook receives). Includes standalone actions (registered directly, with no
# owning container tool_id) plus enumerated verbs kept for defense-in-depth even
# when tool-id resolution is unavailable. Crypto READ verbs are deliberately absent.
#
# 067 P1: a DERIVED view of the per-action policy table (core/verb_policy.py;
# the rows and their per-verb rationale are in core/verb_policy_rows.py) — block
# a verb there with ``correspondent_blocked``. The legacy tool_id tokens
# (``code_execution``, ``hyperliquid``, ...) and the bare venue verbs
# (``place_limit_order``, ...) are RESERVED rows, kept so ``is_high_impact(tool_id)``
# stays truthy and the substring layer below keeps its anchors. The namespaced
# venue trade verbs (``hyperliquid_place_limit_order``, ...) have their own rows,
# so the name layer alone blocks every emitted money write and the substring
# layer is redundant for every emitted verb (pinned by
# tests/unit/core/test_action_name_parity.py); it stays as defense-in-depth.
# ---------------------------------------------------------------------------
from core.lazy_views import lazy_module_getattr, view
from core.verb_policy import ids_where as _verb_ids_where

# 067 P4 prerequisite: LAZY (``core/lazy_views.py``), built on first read after the
# pack loader's phase 1 (the module ``__getattr__`` at the end of this file).
def _high_impact_names():
    return _verb_ids_where(correspondent_blocked=True)


# Back-compat public name HIGH_IMPACT_TOOLS (tests / other callers import it) is the
# SAME object as _HIGH_IMPACT_NAMES.
def _high_impact_tools():
    return view(__name__, "_HIGH_IMPACT_NAMES")

# ---------------------------------------------------------------------------
# Layer 2 — tool_ids whose EVERY action is high-impact, resolved at hook time from
# the action name. Crypto is intentionally absent (its reads must stay allowed; its
# trade verbs are in _HIGH_IMPACT_NAMES). Blocking a high-impact tool's read verbs
# too (e.g. goal_list, cronjob_list, git_log) while tainted is the safe/fail-closed
# direction — the owner clears the taint by replying.
# ---------------------------------------------------------------------------
# WS-2 (2026-07-16): derived from the ONE per-tool capability table
# (core/tool_capabilities.py, `high_impact`) — classify a new tool there, not here;
# per-entry rationale lives with the table rows. Parity-pinned by
# tests/unit/core/test_tool_capabilities.py. The trading venues' deliberate absence is
# now explicit in the table (`readable_while_tainted`).
from core.tool_capabilities import ids_with as _ids_with

# 067 P4 prerequisite: LAZY, like _HIGH_IMPACT_NAMES (the ``__getattr__`` at the end).
def _high_impact_tool_ids():
    return _ids_with("high_impact")

# Substrings that mark a high-impact action even if the exact name isn't enumerated
# (e.g. provider-prefixed MCP/web tools that reach the outside world). NOTE: do NOT add
# "trade" here — as a substring it falsely flags the read action get_trade_history while
# matching no real trade verb (those are enumerated above). F4 (2026-09-14): the same
# reasoning killed the bare "pay" entry — it matched every action whose name merely
# CONTAINS "pay" (e.g. a hypothetical get_payment_history read verb), not just the real
# payer verb, so it is narrowed to the actual namespaced prefix "x402_pay_" (the only
# tool_id whose actions are auto-paying verbs; x402_invoice's request verb is enumerated
# by name above instead, since it isn't namespaced under "x402_pay_").
_HIGH_IMPACT_PREFIXES = ("mcp_", "browser_", "web_", "email_", "x402_pay_", "send_email")

# H10 (2026-07-15): the crypto TRADE verbs, matched as substrings so BOTH the bare
# name AND every venue-namespaced runtime form are caught. Container-tool actions
# register namespaced (polymarket_place_limit_order / hyperliquid_place_market_order),
# and that namespaced name is what reaches the gate hook — but crypto tool_ids are
# deliberately absent from HIGH_IMPACT_TOOL_IDS (reads must stay allowed), so bare-name
# matching alone let the namespaced trade verb slip the gate. Unlike "trade", none of
# these substrings appears in a read verb (reads are get_*). Keep in sync with the trade
# verbs enumerated in _HIGH_IMPACT_NAMES.
_HIGH_IMPACT_VERB_SUBSTRINGS = (
    "place_limit_order", "place_market_order", "cancel_order",
    "cancel_all_orders", "update_leverage", "approve_agent", "revoke_agent",
)


def is_high_impact(action_name: Optional[str]) -> bool:
    """Name-only high-impact test (no tool_id resolution).

    True if ``action_name`` is an enumerated high-impact action name (or legacy
    tool_id token) or matches a high-impact substring. This is the backward-compatible
    surface; the wired hook additionally resolves the owning tool_id via
    :func:`is_high_impact_call`.
    """
    if not action_name:
        return False
    name = str(action_name).strip().lower()
    if name in view(__name__, "_HIGH_IMPACT_NAMES"):
        return True
    if any(sub in name for sub in _HIGH_IMPACT_VERB_SUBSTRINGS):
        return True
    return any(name.startswith(p) or p in name for p in _HIGH_IMPACT_PREFIXES)


def is_high_impact_call(action_name: Optional[str], tool_id: Optional[str] = None) -> bool:
    """Full high-impact decision for one action call.

    Blocks when the bare action name is high-impact (:func:`is_high_impact`) OR when the
    action's owning ``tool_id`` is in :data:`HIGH_IMPACT_TOOL_IDS`. ``tool_id`` is the
    resolved owning tool (``Controller.get_action_details(name).tool``); pass ``None``
    when it can't be resolved, in which case the decision degrades to the name-only path.
    """
    if is_high_impact(action_name):
        return True
    if tool_id and str(tool_id).strip().lower() in view(__name__, "HIGH_IMPACT_TOOL_IDS"):
        return True
    return False


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
        return (str(_param(params, "surface") or ""),
                str(_param(params, "target") or ""))
    if name == "email_send":
        # EmailSendAction is extra="forbid" with a single `to` field — there is no
        # cc/bcc to widen the blast radius. Keep reading both anyway: the check is
        # free, and it stays correct if the param model ever grows them.
        if _param(params, "cc") or _param(params, "bcc"):
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


def build_reply_allowed(
    get_container: Callable[[], Any],
    get_user_id: Callable[[], str],
) -> Callable[[str, str], bool]:
    """Policy for the scoped tainted-reply exemption: flag + rounds budget.

    Denies unless ``CORRESPONDENT_REPLY_ENABLED`` (default OFF) AND the tenant has
    sent fewer than ``CORRESPONDENT_REPLY_MAX_ROUNDS`` outbound messages to that
    address in the last 24h (counted from the ConversationStore). Fail-CLOSED —
    if the budget can't be verified, the reply stays blocked (the owner can
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
                # Flag explicitly ON but no budget substrate — allow (the flag is
                # the operator's informed opt-in; without a store there is no
                # rounds history to enforce).
                return True
            user_id = get_user_id() or ""
            return store.outbound_count_since(user_id, surface, address,
                                              86400) < max_rounds
        except Exception as e:  # fail-closed
            logger.debug("reply_allowed probe failed (deny): %s", e)
            return False
    return _allowed


def make_correspondent_gate_hook(
    get_tainted: Callable[[], bool],
    resolve_tool: Optional[Callable[[str], Optional[str]]] = None,
    get_taint_sources: Optional[Callable[[], Any]] = None,
    reply_allowed: Optional[Callable[[str, str], bool]] = None,
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
    """
    def _hook(action_name: str, params: Any, context: Any) -> Optional[str]:
        tool_id: Optional[str] = None
        if resolve_tool is not None:
            try:
                tool_id = resolve_tool(action_name)
            except Exception as e:  # resolution must never break the gate
                logger.debug("correspondent gate tool resolve failed: %s", e)
                tool_id = None
        if not is_high_impact_call(action_name, tool_id):
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
            record_refusal("correspondent_taint", tool=action_name,
                           user_id=getattr(context, "user_id", None) or "",
                           session_id=getattr(context, "session_id", None) or "")
            return (f"'{action_name}' is blocked: the latest input is untrusted "
                    f"correspondent DATA — owner confirmation is required before a "
                    f"high-impact action.")
        return None
    return _hook


# 067 P4 prerequisite: the name layer, built on first read.
__getattr__ = lazy_module_getattr(__name__, {
    "_HIGH_IMPACT_NAMES": _high_impact_names,
    "HIGH_IMPACT_TOOLS": _high_impact_tools,
    "HIGH_IMPACT_TOOL_IDS": _high_impact_tool_ids,
})
