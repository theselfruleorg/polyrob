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
    "defi_trade_solana_swap",
    # ⚠️ `defi_trade_bridge` is deliberately ABSENT (039). It sat here AND carried
    # its own owner queue in tools/defi/bridge_verb.py, and the two did not know
    # about each other: on 2026-09-12 the owner tapped twice for one bridge, from
    # two prompts describing the same transaction differently. The verb's own ask
    # is strictly the better one — it knows the recipient, the USD value, the
    # arrival floor and the Relay request id, none of which the raw action params
    # carry. ONE gate, and it is that one. The bridge remains in
    # spend_lane.DEFI_SPEND_VERBS, so the ceiling still decides.
    "defi_trade_approve_token",
    "defi_trade_revoke_approval",
    # 2026-09-15: the non-fungible verbs. `nft_transfer` sends an asset NOTHING
    # in this system can price, so it keeps the owner queue in every mode --
    # see ALWAYS_OWNER_APPROVED_VERBS. `nft_revoke_approval` is SPEND-side only
    # in the mechanical sense (a signed transaction from the treasury); it can
    # only retire a standing operator claim, so the tiered lane exempts it as
    # risk-reducing, exactly like `revoke_approval`.
    "defi_trade_nft_transfer",
    "defi_trade_nft_revoke_approval",
    # 046: registering the agent's own ERC-8004 identity. It sends nothing --
    # its cost is the fee -- but it is a signed transaction from the treasury
    # wallet that creates a PERMANENT public identity, and "not really a spend"
    # is exactly the reasoning that left solana_swap and x402_fetch ungoverned.
    "defi_trade_register_agent",
    "defi_trade_set_agent_uri",
    # 2026-09-13: wrapping native into its ERC-20 form. Economically it is not a
    # spend at all -- 1:1, into the SAME wallet, no counterparty and no route --
    # but mechanically it is a signed native-value transaction, and "not really a
    # spend" is exactly the reasoning that left `solana_swap` and `x402_fetch`
    # ungoverned. It is SPEND-side like every sibling; the tiered lane below the
    # ceiling is what keeps it from needing a tap for a routine wrap.
    "defi_trade_wrap",
    "defi_trade_unwrap",
    # 042: deployment and the generic contract call. All three are SPEND-side.
    # `deploy_token` and `deploy_contract` send nothing (the cost is the fee),
    # but "not really a spend" is exactly the reasoning that left `solana_swap`
    # and `x402_fetch` ungoverned, and a deployment is a signed transaction that
    # creates a thing the owner will be asked about. `call` is the widest verb in
    # the tree — it executes calldata the model supplied — so it keeps the tap
    # unless the operator explicitly arms the tiered lane.
    "defi_trade_deploy_token",
    "defi_trade_deploy_contract",
    "defi_trade_call",
    "defi_trade_lp_add", "defi_trade_lp_remove", "defi_trade_lp_collect",
    # 042b: the Solana twin. A mint creation sends nothing, but it pays rent
    # from the treasury and creates an asset the owner will be asked about.
    "defi_trade_solana_deploy_token",
    # 042: the launchpad writes. A launch signs the launch fee plus an opening
    # buy; a curve buy/sell moves value against a protocol we did not write.
    "launchpad_launch",
    "launchpad_buy",
    "launchpad_sell",
    # A claim sends nothing, but it is a signed transaction against a money
    # contract from the treasury wallet, so it stands in the same queue as
    # every other launchpad write. Its cost is the fee.
    "launchpad_claim",
    # 042: connecting the wallet to a dapp page IS the authorization. The spend
    # happens inside a browser callback, not in an action this hook can see, so
    # gating the connect is the only place the owner can be asked at all.
    "dapp_browser_dapp_connect",
    # H1a (audit 2026-08-22): the x402 auto-pay verb shipped on NO approval lane
    # at all — the only money verb with neither owner approval nor a forged-turn
    # refusal. SPEND-side (deliberately absent from PAYMENT_RECEIVE_APPROVAL_TOOLS),
    # so it keeps owner_queue pre-approval in every mode; a micro-payment inside
    # X402_AUTONOMOUS_MAX_USD is waved through by spend_lane._x402_exemption, not
    # by mode.
    "x402_pay_x402_fetch",
)

#: Money verbs that own their approval gate INSIDE the verb rather than through
#: the shared pre-hook above (039). This exists so "every money verb is on an
#: owner approval lane" stays a testable contract with no holes: a verb must be in
#: PAYMENT_APPROVAL_TOOLS **or** here, and the bidirectional test in
#: `tests/unit/tools/defi/test_trade_gating.py` fails on one that is in neither.
#:
#: The entry earns its place only by asking a BETTER question than the generic
#: hook can. `defi_trade_bridge` knows the recipient address, the USD value, the
#: arrival floor and the Relay request id; the pre-hook sees only the raw action
#: params. Carrying both meant two taps for one bridge, from two prompts
#: describing the same transaction differently — which is what the owner hit on
#: 2026-09-12 and what this split ends.
VERB_OWNED_APPROVAL_GATES = {
    "defi_trade_bridge": "tools/defi/bridge_verb.py::_require_owner_approval",
}

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
