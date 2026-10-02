"""Payment-approval action-name lanes (extracted from policy.py; the god-file
ratchet is why this lives here). policy.py re-exports all three names, so every
existing importer (``core.config_policy`` / ``agents.task.constants``) is
unaffected.

067 P1: the three names are DERIVED views of the per-action policy table
(``core/verb_policy.py``; the rows and the per-verb rationale live in
``core/verb_policy_rows.py``). Put a verb on a lane there, by its
``approval_owner``, ``verb_gate`` and ``side`` fields.

067 P4 prerequisite: the three views are LAZY (``core/lazy_views.py``) — built
on first read, not at import, so ``import core`` no longer freezes them before
the pack loader's phase 1 registers pack money rows. The builders below keep
each view's exact predicate and order.
"""
from core.lazy_views import lazy_module_getattr
from core.verb_policy import VERB_POLICY, ordered_ids_where

#: ``approval_owner="hook"``: outward-facing payment-CREATION actions (Task 9,
#: G-2) — gated by PAYMENT_APPROVAL_MODE regardless of the generic
#: APPROVAL_REQUIRED_TOOLS opt-in (payment gating is first-class, not opt-in).
#: ⚠️ RUNTIME action names only — the approval hook matches EXACTLY, and
#: container tools register as {tool_id}_{action}. Pinned by
#: tests/unit/core/test_action_name_parity.py.
def _payment_approval_tools():
    return ordered_ids_where(approval_owner="hook")

#: ``approval_owner="verb"``: money verbs that own their approval gate INSIDE the
#: verb rather than through the shared pre-hook above (039), mapped to that gate.
#: This exists so "every money verb is on an owner approval lane" stays a
#: testable contract with no holes: a verb must be in PAYMENT_APPROVAL_TOOLS
#: **or** here, and the bidirectional test in
#: `tests/unit/tools/defi/test_trade_gating.py` fails on one that is in neither.
#: The entry earns its place only by asking a BETTER question than the generic
#: hook can (``defi_trade_bridge``: recipient, USD value, arrival floor, Relay
#: request id). Carrying both meant two taps for one bridge (2026-09-12).
def _verb_owned_approval_gates():
    return {
        name: VERB_POLICY[name].verb_gate for name in ordered_ids_where(approval_owner="verb")
    }

#: ``side="receive"`` on the hook: 013 T7 review — PAYMENT_APPROVAL_TOOLS is NOT
#: one uniform lane. This carves out the RECEIVE-side subset that is eligible
#: for act-and-report under PAYMENT_APPROVAL_MODE=auto (post-hoc owner notify
#: only, no pre-approval block) — today just the invoicing verb. Every OTHER
#: entry is SPEND-side and ALWAYS keeps owner_queue pre-approval regardless of
#: mode — see tools/controller/service.py's
#: `_spend_tools = _payment_tools - set(this tuple)` wiring. Fail-safe by
#: construction: a new hook verb that is not ``side="receive"`` defaults to the
#: strict (pre-approved) lane, never silently to act-and-report. The hard
#: product line (proposal 013) is money-spend/trading is NEVER act-and-report,
#: even under an explicit PAYMENT_APPROVAL_MODE=auto.
def _payment_receive_approval_tools():
    return ordered_ids_where(approval_owner="hook", side="receive")


#: The lazy public names (a module ``__getattr__``, PEP 562).
LAZY_VIEWS = ("PAYMENT_APPROVAL_TOOLS", "PAYMENT_RECEIVE_APPROVAL_TOOLS",
              "VERB_OWNED_APPROVAL_GATES")

__getattr__ = lazy_module_getattr(__name__, {
    "PAYMENT_APPROVAL_TOOLS": _payment_approval_tools,
    "PAYMENT_RECEIVE_APPROVAL_TOOLS": _payment_receive_approval_tools,
    "VERB_OWNED_APPROVAL_GATES": _verb_owned_approval_gates,
})
