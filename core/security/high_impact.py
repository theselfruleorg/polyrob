"""The high-impact classification: which action calls a tainted or public session may not run.

One policy, two gates: the correspondent taint gate
(``agents/task/agent/core/correspondent_gate.py``) and the public-room gate
(``core/surfaces/room_policy.py``) both ask :func:`is_high_impact_call`. It
moved here from the correspondent gate (proposal 125 S21f) so the room gate
reads it downward; the correspondent gate re-exports every name.

⚠️ **tool_id vs action-name (the load-bearing detail).** A pre-tool-call hook
receives the bare *action name* — ``run_code``, ``goal_create``, ``x402_fetch`` —
never the *tool_id* (``code_execution``, ``goal``, ``x402_pay``). So this module
classifies in two layers:

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

Pure policy; tier-0 imports only (``core.lazy_views``, ``core.verb_policy``,
``core.tool_capabilities``).
"""
from typing import Optional

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
# tests/unit/core/test_tool_capabilities.py.
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


# 067 P4 prerequisite: the name layer, built on first read.
__getattr__ = lazy_module_getattr(__name__, {
    "_HIGH_IMPACT_NAMES": _high_impact_names,
    "HIGH_IMPACT_TOOLS": _high_impact_tools,
    "HIGH_IMPACT_TOOL_IDS": _high_impact_tool_ids,
})
