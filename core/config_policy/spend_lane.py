"""The tiered on-chain spend lane (proposal 023 §5.3 D3).

`PAYMENT_APPROVAL_TOOLS` puts every `defi_trade` verb on the durable
`owner_queue` pre-approval lane in EVERY mode. That is the right default —
an on-chain send is irreversible and self-custodial, with no facilitator to
dispute it with — but it has two costs the owner may deliberately choose to
pay:

1. It gates the SIMULATION too. The hook matches on action name alone, so a
   `dry_run=True` swap — which returns from `_run_guarded` *before*
   `sign_and_send` and provably broadcasts nothing — also waits on an owner
   tap. Blocking a quote buys no safety at all, so the dry-run exemption here
   is UNCONDITIONAL.
2. It makes unattended trading impossible. A goal that runs at 03:00 blocks
   on a tap that is not coming, times out after
   ``payment_approval_timeout_sec()`` and is denied.

`DEFI_TIERED_SPEND_LANE` (default OFF) is the explicit owner decision the
023 T4 note reserved: below the per-transaction autonomous ceiling
(``DEFI_AUTONOMOUS_MAX_USD``) a live spend runs act-and-report; above it the
owner queue is unchanged. Nothing else is relaxed — ``tx_guard`` still
simulates every transaction, asserts the observed asset and allowance deltas
against the declared intent, applies PolicyGate and the rolling daily cap,
and independently re-refuses anything over the SAME ceiling with
``lane="owner_queue"``. The declared ``max_spend_usd`` is a sound tiering key
precisely because tx_guard holds the caller to it: a call that under-declares
to buy its way into this lane is refused by the simulation, not executed.

Pure policy — no imports beyond the env helpers, so every tier can read it.
"""
from typing import Any, Dict, Optional

from core.env import bool_env, float_env

#: The on-chain spend verbs this lane can exempt. RUNTIME action names —
#: container tools register as ``{tool_id}_{action}``. Deliberately NOT the
#: venue order verbs (hyperliquid/polymarket) or ``x402_pay``: this lane is
#: scoped to the simulate-and-assert paths — tx_guard for the EVM verbs, and
#: solana_swap's mirrored guard (2026-08-27), which values the simulated
#: outflow and holds it to the declared ``max_spend_usd`` before signing.
DEFI_SPEND_VERBS = frozenset({
    "defi_trade_swap",
    # 2026-09-12: the bridge JOINED this set. It sat in
    # ALWAYS_OWNER_APPROVED_VERBS on the reasoning that it can relocate the
    # treasury in one action; the owner's verdict after actually using it was
    # that a money rail needing a tap per move is not autonomy at all. Caps, not
    # taps — the per-tx ceiling, the rolling daily cap and the simulated,
    # asserted deltas bound it exactly as they bound every verb below, and the
    # owner queue still catches anything over DEFI_AUTONOMOUS_MAX_USD.
    "defi_trade_bridge",
    "defi_trade_solana_swap",
    "defi_trade_transfer",
    "defi_trade_approve_token",
    "defi_trade_revoke_approval",
    # 2026-09-15: retiring a blanket operator approval on an NFT collection.
    # It grants nothing and moves nothing; its only effect is to remove a
    # standing claim on the wallet. Making the owner tap to REDUCE risk is how
    # a wallet stays exposed.
    "defi_trade_nft_revoke_approval",
    # 046: ERC-8004 identity. Unlike an NFT transfer, a registration's whole
    # cost IS a bounded fee that tx_guard prices and holds to max_spend_usd, so
    # the ceiling is meaningful and the capped lane is the right one. Above it
    # the owner queue still catches the act, which is the default posture for a
    # permanent public identity.
    "defi_trade_register_agent",
    "defi_trade_set_agent_uri",
    # Wrapping native -> wrapped native. The most benign verb in this set: the
    # destination is the chain registry's PINNED wrapped_native (never caller
    # supplied), the rate is 1:1 by the contract's definition, and the value
    # never leaves the wallet -- it changes form. tx_guard still simulates it and
    # asserts the measured native outflow against the declared amount.
    "defi_trade_wrap",
    "defi_trade_unwrap",
    # 042. Caps, not taps — the same posture the owner set for the bridge.
    # A deployment's whole cost is its fee plus whatever native it endows, both
    # priced and both held to the declared `max_spend_usd` by tx_guard; `call`
    # is held to a declared outflow AND a declared MINIMUM INFLOW, so a call
    # that spends and returns nothing never reaches a signature. Above the
    # ceiling all three still reach the owner queue.
    "defi_trade_deploy_token",
    "defi_trade_deploy_contract",
    "defi_trade_call",
    "defi_trade_lp_add", "defi_trade_lp_remove", "defi_trade_lp_collect",
    # 042b: the Solana twin. Bounded by measured rent (~0.0035 SOL) held to the
    # declared max_spend_usd, and by the same ceiling above it.
    "defi_trade_solana_deploy_token",
    # 042: the launchpad writes. Caps, not taps -- each is a bounded spend the
    # guard simulates and asserts, and the owner queue still catches anything
    # over DEFI_AUTONOMOUS_MAX_USD.
    "launchpad_launch",
    "launchpad_buy",
    "launchpad_sell",
    # Caps, not taps: a claim is bounded by its FEE, and the money moves
    # TOWARD the treasury. Making the agent ask permission to collect its
    # own revenue is the shape the owner rejected on 2026-09-12.
    "launchpad_claim",
    # 042: the dapp connect. Bounded by a declared per-transaction ceiling AND
    # a session budget, with every individual transaction simulated and asserted
    # by tx_guard inside the bridge.
    "dapp_browser_dapp_connect",
})

#: Money-spend verbs that are NEVER exemptible by this lane, and the reason.
#: A verb must be in DEFI_SPEND_VERBS or here — the bidirectional contract test
#: (`tests/unit/core/test_money_verb_registration.py`) fails on one that is in
#: neither, so a new verb cannot be forgotten into the wrong lane.
#:
#: `bridge` (037): the owner's explicit decision on 2026-09-11 was that a bridge
#: is ALWAYS owner-approved with NO dollar cap, because it is the only money verb
#: that can relocate the whole treasury in one action. Putting it in
#: DEFI_SPEND_VERBS would let `DEFI_TIERED_SPEND_LANE=true` exempt it below a
#: ceiling — i.e. one env flag would silently overturn that decision. It is also
#: not simulate-and-assert in the single-transaction sense this lane is scoped to:
#: its proof is the ARRIVAL on another chain (core/wallet/bridge_guard.py).
#: Empty since 2026-09-12 — the bridge moved to :data:`DEFI_SPEND_VERBS` above.
#: KEPT as the classification seam rather than deleted: the bidirectional
#: contract test requires every defi spend verb to sit in one bucket or the
#: other, so a future always-approve verb has a home and cannot be forgotten
#: into the exemptible set by default.
ALWAYS_OWNER_APPROVED_VERBS = frozenset({
    # 2026-09-15: sending a non-fungible. ⚠️ This is the first genuine member,
    # and it is the shape the bucket was kept for.
    #
    # Every other verb here is bounded by a USD figure the guard can compute and
    # hold the caller to. An NFT has NO reliable price: a floor is thin,
    # trivially wash-traded, and often absent entirely. `tx_guard` therefore
    # prices the TRANSACTION at its worst-case fee -- true, and no bound at all
    # on what is being sent. A cap that cannot bound the thing it is capping is
    # worse than no cap, because it reads as protection.
    #
    # So the OWNER is the bound. Putting this in DEFI_SPEND_VERBS instead would
    # let one env flag (`DEFI_TIERED_SPEND_LANE=true`) wave through the transfer
    # of an asset of unknown value under a $25 ceiling it has no relation to.
    "defi_trade_nft_transfer",
})

#: ``revoke_approval`` sets an allowance to ZERO. It grants nothing and moves
#: no token; its only effect is to retire a standing claim on the wallet. It
#: carries no ``max_spend_usd`` for that reason, so it is tiered on identity.
_RISK_REDUCING_VERBS = frozenset({
    "defi_trade_lp_remove", "defi_trade_lp_collect","defi_trade_revoke_approval",
                                  "defi_trade_nft_revoke_approval"})

#: The x402 auto-pay verb. It is on `PAYMENT_APPROVAL_TOOLS` (H1a) so an
#: above-ceiling payment reaches the owner queue, but a micro-payment must not:
#: blocking every $0.001 fetch on an owner tap makes x402 unusable and times out
#: on any unattended run.
X402_SPEND_VERBS = frozenset({"x402_pay_x402_fetch"})


def tiered_spend_lane_enabled() -> bool:
    """Whether a within-ceiling live spend may skip the owner-queue tap."""
    return bool_env("DEFI_TIERED_SPEND_LANE", False)


def autonomous_ceiling_usd() -> float:
    """The per-transaction autonomous ceiling — the SAME number tx_guard step 9
    reads, resolved the SAME way (owner pref ``budget.defi_autonomous_usd`` over
    ``DEFI_AUTONOMOUS_MAX_USD``, clamped to the per-tx backstop).

    Until 2026-09-18 this read the RAW env var while tx_guard read the pref, so
    the two halves of one lane disagreed: prod's owner approved a $300
    autonomous ceiling from chat, tx_guard honoured it, and this hook still
    demanded a tap for every live swap over the env's $5 — three taps in one
    afternoon for trades the owner had already said may run unattended, and an
    unattended cron buyback that could never run at all. Delegating to
    tx_guard's own resolver keeps the two in sync by construction; a failed
    resolve falls back to the env value, never to a wider one.
    """
    env_value = float_env("DEFI_AUTONOMOUS_MAX_USD", 25.0)
    try:
        # Lazy: core.wallet.tx_guard imports core.config_policy at module load.
        from core.wallet.tx_guard import autonomous_max_usd, ceiling_scope
        return float(autonomous_max_usd(*ceiling_scope(None)))
    except Exception:
        return env_value


def x402_autonomous_ceiling_usd() -> float:
    """Per-payment ceiling below which an x402 fetch runs act-and-report.

    Deliberately far smaller than DEFI_AUTONOMOUS_MAX_USD: an x402 payment is
    NOT tx_guard-simulated. Its only bounds are this ceiling, the PolicyGate
    per-tx ceiling, and the rolling daily cap — so the autonomous slice must be
    small enough that a looped drain is bounded by the daily cap long before it
    matters.
    """
    return float_env("X402_AUTONOMOUS_MAX_USD", 1.0)


def _is_simulation(params: Dict[str, Any]) -> bool:
    """True when this call cannot broadcast.

    ``dry_run`` defaults to **True** on every spend param model, so an ABSENT
    key means simulate. `tests/unit/core/test_spend_lane.py` pins those
    defaults, so a model that ever flipped one to False fails the suite rather
    than silently widening this exemption.
    """
    return bool(params.get("dry_run", True))


def spend_exemption(action_name: str,
                    params: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why this call may bypass the owner queue — or None to keep the tap.

    None is the safe answer: the caller treats it as "gate normally", so every
    unrecognised verb, malformed param set and undeclared amount keeps today's
    behaviour.

    Two families, deliberately NOT unified:
      * ``DEFI_SPEND_VERBS`` — tx_guard-simulated; exempt on dry-run
        unconditionally, and on a declared ceiling only under
        ``DEFI_TIERED_SPEND_LANE``.
      * ``X402_SPEND_VERBS`` — NOT simulated; exempt only below the much smaller
        ``X402_AUTONOMOUS_MAX_USD``, with no flag, because the alternative
        default is "every micro-payment blocks".
    """
    params = params or {}
    if action_name in X402_SPEND_VERBS:
        return _x402_exemption(params)
    if action_name not in DEFI_SPEND_VERBS:
        return None

    if _is_simulation(params):
        return ("dry run — the guard is consulted but nothing is broadcast")

    if not tiered_spend_lane_enabled():
        return None

    if action_name in _RISK_REDUCING_VERBS:
        return "revoke — sets the allowance to zero, so it can only reduce risk"

    declared = params.get("max_spend_usd")
    if not isinstance(declared, (int, float)) or isinstance(declared, bool):
        return None
    if declared <= 0:
        return None

    ceiling = autonomous_ceiling_usd()
    if declared <= ceiling:
        return (f"declared ceiling ${declared:.4f} is within the ${ceiling:.2f} "
                f"autonomous limit")
    return None


#: Back-compat alias — `tools/controller/service.py` and existing tests import
#: this name. Kept so the rename is not a breaking change for any importer.
defi_spend_exemption = spend_exemption


def _x402_exemption(params: Dict[str, Any]) -> Optional[str]:
    """x402 auto-pay: exempt only strictly inside the small autonomous ceiling.

    Keyed on ``max_amount_usd`` — the agent's own declared authorization — which
    is a sound tiering key here ONLY because `tools/x402/service.py` authorizes
    the PolicyGate at exactly that worst case (`check_amount = max_amount_usd`),
    so a call cannot under-declare to buy into this lane and then pay more.
    """
    declared = params.get("max_amount_usd")
    if not isinstance(declared, (int, float)) or isinstance(declared, bool):
        return None
    if declared <= 0:
        return None
    ceiling = x402_autonomous_ceiling_usd()
    if declared <= ceiling:
        return (f"x402 micro-payment: declared ceiling ${declared:.4f} is within "
                f"the ${ceiling:.2f} autonomous limit")
    return None
