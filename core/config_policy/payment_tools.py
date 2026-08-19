"""Payment-approval action-name lanes (extracted from policy.py — pure data,
no imports; the god-file ratchet is why this lives here). policy.py re-exports
both names, so every existing importer (``core.config_policy`` /
``agents.task.constants``) is unaffected."""

# Task 9 (G-2): outward-facing payment-CREATION actions — gated by PAYMENT_APPROVAL_MODE
# regardless of the generic APPROVAL_REQUIRED_TOOLS opt-in (payment gating is first-class,
# not opt-in). A future subscription-renewal verb joins this tuple, not a new mode.
PAYMENT_APPROVAL_TOOLS = (
    # ⚠️ RUNTIME action names only — the approval hook matches EXACTLY, and container
    # tools register as {tool_id}_{action}. A bare `x402_request` here matched nothing,
    # so this lane never fired. Pinned by tests/unit/core/test_action_name_parity.py.
    "x402_invoice_x402_request",
    # L9 (2026-07-15): live-trade order verbs are money-moving too — a within-cap
    # live order gets the SAME owner-in-the-loop an invoice does, not unattended.
    "hyperliquid_place_limit_order",
    "hyperliquid_place_market_order",
    "polymarket_place_limit_order",
    "polymarket_place_market_order",
    # 023 T3: the on-chain money verb. Irreversible and self-custodial — there is
    # no exchange or facilitator to dispute it with — so it is SPEND-side and
    # never act-and-report, in any mode.
    "defi_trade_transfer",
    # 023 T4 (2026-08-14 review fix): the swap/allowance verbs shipped on NO
    # lane at all — they bypassed owner approval entirely while transfer was
    # gated. All three are SPEND-side like transfer: a swap moves value out,
    # and an approval is a standing claim on the wallet (the drain happens in
    # a later transaction). NOTE this is deliberately STRICTER than proposal
    # 023 §5.3's D3 tiered lane (act-and-report below caps), which was never
    # implemented — loosening to D3 is an explicit owner decision, not a
    # default.
    "defi_trade_swap",
    "defi_trade_approve_token",
    "defi_trade_revoke_approval",
)

# 013 T7 review (Important finding fix): PAYMENT_APPROVAL_TOOLS is NOT one uniform
# lane. This tuple carves out the RECEIVE-side subset that is eligible for
# act-and-report under PAYMENT_APPROVAL_MODE=auto (post-hoc owner notify only, no
# pre-approval block) — today just the invoicing verb. Every OTHER entry in
# PAYMENT_APPROVAL_TOOLS (the live-trade order verbs above) is treated as
# SPEND-side and ALWAYS keeps owner_queue pre-approval regardless of mode — see
# tools/controller/service.py's `_spend_tools = _payment_tools - set(this tuple)`
# wiring. This is a fail-safe by construction: a future addition to
# PAYMENT_APPROVAL_TOOLS that is NOT also added here defaults to the strict
# (pre-approved) lane, never silently to act-and-report. The hard product line
# (proposal 013) is money-spend/trading is NEVER act-and-report, even under an
# explicit PAYMENT_APPROVAL_MODE=auto.
PAYMENT_RECEIVE_APPROVAL_TOOLS = (
    "x402_invoice_x402_request",  # runtime (namespaced) name — see above
)
