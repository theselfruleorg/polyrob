"""THE choke point. No value-moving transaction may be broadcast without a
``Decision(allowed=True)`` from this module.

The bound is NOT "we only wrote safe verbs" — that stops being a security
property the moment the agent can supply its own calldata. The bound is: every
state-changing transaction is simulated, its asset and allowance deltas are
asserted against a DECLARED intent, and it is refused if the simulation
disagrees with the declaration. Screening, denylists, caps and approval lanes
are defence in depth layered on that.

What this mechanism genuinely cannot see, recorded so nobody mistakes the bound
for a guarantee:

* **The declaration and the calldata can share an author.** Delta assertion
  proves the transaction does what the intent SAYS; it cannot prove the intent
  is legitimate. If a prompt injection authors both, they will agree. The
  defences against that are turn-origin refusal and the caps, not this.
* **A signature is not a transaction.** An EIP-2612/Permit2 payload is signed
  and submitted by someone else later, so it never reaches this function. That
  is why ``Signer.sign_typed_data`` is kept off the money path.
* **The RPC is the oracle.** Simulation, deltas and caps all read from one
  endpoint. Hence the refusal to arm on the shared public endpoint.

Fail-closed is not configurable here: any probe failure refuses.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Tuple

from core.env import bool_env, float_env
from core.wallet import simulation

logger = logging.getLogger(__name__)

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
_DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"

#: Native balance is not expected to move on an ERC-20 transfer (eth_call does
#: not charge gas), so any material native delta means the transaction does
#: something the caller did not declare.
_NATIVE_DUST_WEI = 10 ** 12


@dataclass(frozen=True)
class TxIntent:
    """What the caller SAYS the transaction will do. Asserted, never trusted."""
    chain: str
    token: Optional[str]          # None = native transfer
    to: str
    amount_raw: int
    max_spend_usd: float
    expected_allowance_grants: Sequence[Tuple[str, str, int]] = ()
    #: Spenders to MEASURE without declaring a grant (e.g. the router a swap
    #: pulls through). A measured pair is judged by its read delta — increases
    #: refuse unless declared, decreases are fine — while an Approval EVENT on
    #: an unmeasured pair refuses outright. Without this, an OZ-4.x-style
    #: token that re-emits Approval(remaining) on transferFrom would read as a
    #: hidden grant on every legitimate swap.
    watch_spenders: Sequence[str] = ()
    idempotency_key: Optional[str] = None
    #: True for an approve/revoke (023 T4). Such a call transfers NOTHING — its
    #: risk is the allowance, declared above and verified against the simulated
    #: delta. Without this the structural "amount must be > 0" TRANSFER rule
    #: refused every approval outright (found on prod 2026-08-14; the unit tests
    #: stub the guard, so they could not see it).
    is_allowance_op: bool = False
    #: The wallet's OWN on-chain balance of `token` at call time (028, 2026-08-22).
    #: An allowance grant on `token` that does not exceed this is EXIT-bounded —
    #: it cannot put more at risk than the wallet already holds, because the
    #: spender can never move more than the approved amount and the approved
    #: amount is capped here at what is already owned. This lets a priced-at-
    #: entry position that later loses DexScreener "high" confidence still be
    #: approved for exit (fallback_price_fn values it for the cap checks below;
    #: the exemption is what removes the *requirement* of a high-confidence
    #: price, not the requirement of SOME price). None (default) = no exemption,
    #: byte-identical to pre-028 behaviour — the caller must opt in.
    held_balance_raw: Optional[int] = None
    #: The token the transaction is expected to RECEIVE (a swap's token_out),
    #: declared so the guard can MEASURE it (2026-08-26 exit untying). Two jobs:
    #: (a) when the outflow token has no price by ANY source, an exit within
    #: held balance is valued at the measured inflow of the chain's quote asset
    #: — the treasury's receipt is exact and unfalsifiable, so the caps run
    #: against it instead of dead-ending on "unpriceable"; (b) it is what makes
    #: an intent recognizably EXIT-shaped for the DEFI_MONITOR_EXITS lane.
    #: None (default) = byte-identical legacy behaviour.
    inflow_token: Optional[str] = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    lane: str = "refuse"          # "autonomous" | "owner_queue" | "refuse"
    amount_usd: Optional[float] = None
    #: gasUsed measured by the simulation, carried out so the rail can size
    #: the broadcast gas limit from it (EvmRail.size_gas). None = unmeasured.
    sim_gas_used: Optional[int] = None


def autonomous_max_usd() -> float:
    return float_env("DEFI_AUTONOMOUS_MAX_USD", 25.0)


def rpc_is_pinned(chain: str) -> bool:
    """True when the operator has pinned a real endpoint for *chain*.

    Money paths refuse to arm on the shared public default: it is
    unauthenticated and rate-limited, and it is simultaneously the source of the
    simulation, the deltas and the balances the caps are computed from.
    """
    return bool(os.getenv(f"DEFI_EVM_RPC_{chain.upper()}", "").strip())


def _halted() -> bool:
    from core.config_policy import AutonomyConfig
    return AutonomyConfig.autonomy_halted()


def _entry_paused() -> bool:
    from core.config_policy import AutonomyConfig
    return AutonomyConfig.entry_paused()


# NOTE: turn-origin detection lives in the TOOLS tier
# (`tools/controller/action_registration.py::_is_forged_or_autonomous_turn`), and
# core (tier 0) may not import tools (tier 3) — the layering ratchet's upward
# allowlist may only shrink. So the caller INJECTS `forged_fn`. When an
# execution_context is present and no detector was supplied we cannot prove the
# turn is genuine, so we refuse: fail-closed by construction rather than by
# remembering to pass an argument.


def autonomous_turn_trading_enabled() -> bool:
    """Whether a GOAL/CRON-dispatched run may move funds (default OFF).

    ``forged_fn`` answers one question — "is this turn anything other than a
    genuine owner turn?" — and step 2 refuses on it. That is right for a leaf
    sub-agent, a self-wake and a delegation-result re-entry, but it also covers
    a goal-dispatched run, which is what an unattended treasury loop IS. With
    this OFF (the default) the agent can only trade when the owner is driving.

    ⚠️ Turning it ON is a real widening of the money surface, not a formality.
    An autonomous run reads untrusted material (web pages, social posts, market
    APIs) in the SAME session that holds the money verb, so an indirect prompt
    injection reaches a signing path it otherwise never could. What bounds the
    damage is not this gate but the caps underneath it — the per-transaction
    ceiling, the rolling daily cap, and the simulation that holds the caller to
    its declared intent. Enable it only on a treasury you can afford to lose
    entirely in one day, and size ``WALLET_DAILY_CAP_USD`` as exactly that
    number.
    """
    return bool_env("DEFI_AUTONOMOUS_TURN_TRADING", False)


def monitor_exits_enabled() -> bool:
    """Whether a forged MAIN-agent turn (self-wake / delegation-result — the
    monitor loop) may execute EXIT-shaped operations (default OFF).

    A week of prod logs (2026-08-25/26) showed stop rules firing on monitor
    ticks and the turn-origin bar refusing the very exit the rule demanded —
    the position then rode further down until the next goal-dispatched run.
    The worst an adversary steering a forged turn can do through this lane is
    close a position EARLY into the treasury's own quote-asset balance: a
    market cost, not a theft. Entries, transfers, and over-held grants stay
    refused; the caps, the simulation and the delta assertion all still apply,
    and the lane rides the autonomous origin so it also demands a daily cap.
    """
    return bool_env("DEFI_MONITOR_EXITS", False)


def _chain_quote_asset(chain: str) -> Optional[str]:
    """The chain's pinned quote asset: USDC, else the wrapped native (a chain
    like robinhood quotes in WETH and pins no stablecoin)."""
    try:
        from core.wallet import chains
        row = chains.get(chain)
    except Exception:
        return None
    if row is None:
        return None
    return row.usdc or row.wrapped_native


def _intent_is_exit_shaped(intent: TxIntent) -> bool:
    """True when the DECLARED intent can only CLOSE a position, never open one.

    - a revoke (allowance op, no grants) is always an exit;
    - an approve whose every grant is on the held token and within the held
      balance is exit-bounded;
    - a swap that sends the held token (within held balance) and declares the
      chain's QUOTE ASSET as its inflow is a sell-to-quote.
    The declaration is not trusted on its own: the simulation still asserts the
    deltas, and the sell case additionally requires a measured quote inflow.
    """
    if intent.is_allowance_op:
        if not intent.expected_allowance_grants:
            return True  # revoke
        if intent.held_balance_raw is None:
            return False
        return all(
            g_token.lower() == (intent.token or "").lower()
            and g_amount <= intent.held_balance_raw
            for (g_token, _s, g_amount) in intent.expected_allowance_grants)
    quote = _chain_quote_asset(intent.chain)
    return bool(
        quote and intent.inflow_token
        and intent.inflow_token.lower() == quote.lower()
        and intent.token
        and intent.token.lower() != quote.lower()
        and intent.held_balance_raw is not None
        and intent.amount_raw <= intent.held_balance_raw)


def _autonomous_turn_allowed(execution_context, tool_self, autonomous_ok_fn) -> bool:
    """True only for a goal/cron-dispatched MAIN-agent turn, with the flag on.

    Fails CLOSED on every uncertainty: flag off, no detector supplied, detector
    says no, or detector raises. The detector is injected for the same layering
    reason ``forged_fn`` is, and it must be STRICTER than ``forged_fn`` — it
    answers "is this specifically an autonomous goal run?", never merely "is
    this not a genuine owner turn?".
    """
    if not autonomous_turn_trading_enabled():
        return False
    if autonomous_ok_fn is None:
        return False
    try:
        return bool(autonomous_ok_fn(execution_context, tool_self))
    except Exception:
        return False


def authorize(intent: TxIntent, tx: dict, *, holder: str, gate,
              execution_context=None, tool_self=None,
              simulate_fn: Optional[Callable] = None,
              price_fn: Optional[Callable] = None,
              fallback_price_fn: Optional[Callable] = None,
              rpc_is_pinned_fn: Optional[Callable] = None,
              halted_fn: Optional[Callable] = None,
              entry_paused_fn: Optional[Callable] = None,
              forged_fn: Optional[Callable] = None,
              autonomous_ok_fn: Optional[Callable] = None) -> Decision:
    """Authorize *tx* against *intent*. Returns a Decision; never broadcasts.

    The caller must hold ``gate.reserve()`` across authorize → broadcast →
    record so a concurrent transaction cannot both clear a nearly-exhausted cap.
    """
    simulate_fn = simulate_fn or (lambda **kw: simulation.simulate(**kw))
    rpc_is_pinned_fn = rpc_is_pinned_fn or rpc_is_pinned
    halted_fn = halted_fn or _halted
    entry_paused_fn = entry_paused_fn or _entry_paused

    # -- 1. Owner kill-switch (fail closed) -------------------------------
    try:
        if halted_fn():
            return Decision(False, "refused: autonomy is HALTED (owner kill-switch)")
    except Exception as exc:
        return Decision(False, f"refused: kill-switch probe failed ({exc}); failing closed")

    # -- 1b. Owner entry-pause (fail closed; exits always pass) ------------
    # 2026-08-28: give the entry-pause a structural home instead of leaving
    # it to each goal's own prose (one stream-manifest goal traded through it
    # unnoticed). Exit-shaped intents (sells to quote, revokes) are never
    # blocked — the pause is about not adding NEW risk.
    try:
        if entry_paused_fn() and not _intent_is_exit_shaped(intent):
            return Decision(False, (
                "refused: new treasury entries are PAUSED (owner entry-pause) "
                "— exits still run; clear with `polyrob owner resume-entries`"))
    except Exception as exc:
        return Decision(False, f"refused: entry-pause probe failed ({exc}); failing closed")

    # -- 2. Turn origin ----------------------------------------------------
    autonomous_origin = False
    monitor_exit = False
    if execution_context is not None:
        if forged_fn is None:
            return Decision(False, (
                "refused: an agent turn was supplied but no turn-origin detector — "
                "cannot prove the turn is genuine, so failing closed"))
        try:
            if forged_fn(execution_context, tool_self):
                if not _autonomous_turn_allowed(execution_context, tool_self,
                                                autonomous_ok_fn):
                    # 2026-08-26 exit untying: a forged MAIN-agent turn — the
                    # self-wake monitor loop — may still CLOSE a position when
                    # the operator armed DEFI_MONITOR_EXITS. Stop rules fire on
                    # monitor ticks; refusing the exit here left fired stops
                    # unexecutable until the next goal run. Exit-shaped only,
                    # main agent only; a sell additionally has its measured
                    # quote inflow asserted after simulation.
                    if (monitor_exits_enabled()
                            and not getattr(execution_context, "is_sub_agent", False)
                            and getattr(execution_context, "role", "leaf") == "orchestrator"
                            and _intent_is_exit_shaped(intent)):
                        monitor_exit = True
                        logger.info(
                            "tx_guard.monitor_exit chain=%s token=%s — forged turn "
                            "allowed for an EXIT-shaped operation (DEFI_MONITOR_EXITS)",
                            intent.chain, intent.token)
                    else:
                        return Decision(False, (
                            "refused: a forged/autonomous turn (self-wake, delegation-result, "
                            "leaf, or autonomous run) cannot move funds"))
                # A genuine owner turn is not forged; only the unattended
                # goal-dispatched lane (or the monitor-exit lane above) reaches
                # here forged-but-allowed. Remember it so step 9 can demand an
                # aggregate damage bound (a daily cap) that an owner-driven
                # trade doesn't need.
                autonomous_origin = True
        except Exception as exc:
            return Decision(False, f"refused: could not prove the turn is genuine ({exc})")

    # -- 3. Structural -----------------------------------------------------
    if intent.amount_raw < 0:
        return Decision(False, "refused: amount cannot be negative")
    if intent.amount_raw == 0 and not intent.is_allowance_op:
        # A zero-value transfer is meaningless. An allowance op legitimately
        # moves nothing and must say so explicitly — it is never inferred.
        return Decision(False, "refused: amount must be greater than zero")
    if intent.to.lower() in (_ZERO_ADDRESS.lower(), _DEAD_ADDRESS.lower()):
        return Decision(False, "refused: destination is the zero/burn address")

    # -- 4. RPC trust ------------------------------------------------------
    if not rpc_is_pinned_fn(intent.chain):
        return Decision(False, (
            f"refused: no pinned RPC for {intent.chain}. Simulation, deltas and caps "
            f"all read from it, so the shared public endpoint cannot be the trust "
            f"anchor for moving funds — set DEFI_EVM_RPC_{intent.chain.upper()}"))

    # -- 5. Simulate -------------------------------------------------------
    spenders = sorted({s for (_t, s, _a) in intent.expected_allowance_grants}
                      | set(intent.watch_spenders))
    tokens = [t for t in (intent.token, intent.inflow_token) if t]
    try:
        deltas = simulate_fn(tx=tx, holder=holder, chain=intent.chain,
                             tokens=tokens, spenders=spenders)
    except Exception as exc:
        return Decision(False, f"refused: simulation raised ({exc})")
    if not deltas.ok:
        return Decision(False, f"refused: simulation not trustworthy — {deltas.error}")

    # -- 6. Assert the deltas against the declaration ----------------------
    outflow_raw = 0
    if intent.token:
        moved = deltas.token_deltas.get(intent.token)
        if moved is None:
            return Decision(False, "refused: simulation did not measure the token being sent")
        if intent.is_allowance_op:
            # An approve/revoke changes an allowance and must move NOTHING.
            # (Before this branch the zero delta below read as a measurement
            # failure, so with a pinned RPC every approve/revoke was dead on
            # arrival — the second DOA hole on this path; the first was the
            # structural amount>0 rule, fixed by is_allowance_op itself.)
            if moved != 0:
                return Decision(False, (
                    f"refused: an allowance operation moved {moved} of the token — "
                    f"an approve/revoke must not transfer funds"))
        else:
            if moved > 0:
                return Decision(False, "refused: simulation shows an INFLOW for a send")
            if moved == 0:
                # A send that moves nothing means the MEASUREMENT failed, not that
                # the transfer is free. This exact hole broadcast a live 0.25 USDC
                # transfer on 2026-08-09: eth_call does not persist state, so every
                # delta read back as 0, the outflow priced at $0.00, and it sailed
                # through a cap that should have refused it. Zero is never a cheap
                # transfer — it is an unmeasured one.
                return Decision(False, (
                    "refused: the simulation measured NO outflow for a transfer that "
                    "should move funds — treating that as a measurement failure, not "
                    "as a free transaction"))
            outflow_raw = -moved
            if outflow_raw > intent.amount_raw:
                return Decision(False, (
                    f"refused: simulated outflow {outflow_raw} exceeds the declared "
                    f"amount {intent.amount_raw}"))

    declared = {(t.lower(), s.lower()): a for (t, s, a) in intent.expected_allowance_grants}
    for (token, spender), change in deltas.allowance_deltas.items():
        if change <= 0:
            continue
        permitted = declared.get((token.lower(), spender.lower()))
        if permitted is None:
            return Decision(False, (
                f"refused: UNDECLARED allowance grant of {change} on {token} to "
                f"{spender} — a hidden approve is not bounded by a USD cap, because "
                f"the drain happens in a later transaction"))
        if change > permitted:
            return Decision(False, (
                f"refused: allowance grant {change} exceeds the declared {permitted}"))

    # -- 6b. Event-log cross-check ----------------------------------------
    # The reads above cover ONLY the declared token and the measured spenders;
    # a grant to an undeclared spender or a drain of an undeclared token was
    # invisible to them (found in the 2026-08-14 T4 review). The simulated
    # tx's own event log covers whoever it actually touched. Measured pairs
    # are judged by their read delta (authoritative — some tokens re-emit
    # Approval(remaining) on transferFrom, which is a decrease, not a grant);
    # an Approval EVENT on an UNMEASURED pair refuses outright.
    measured_pairs = {(t.lower(), s.lower()) for (t, s) in deltas.allowance_deltas}
    for (l_token, l_spender, l_amount) in deltas.holder_approvals:
        if l_amount <= 0:
            continue                      # setting an allowance to 0 is a revoke
        if (l_token.lower(), l_spender.lower()) in measured_pairs:
            continue                      # the read delta above already judged it
        return Decision(False, (
            f"refused: the transaction emits an UNDECLARED Approval of {l_amount} "
            f"on {l_token} to {l_spender} — a hidden approve is not bounded by a "
            f"USD cap, because the drain happens in a later transaction"))
    for (l_token, _l_to, l_amount) in deltas.holder_transfers:
        if intent.token and l_token.lower() == intent.token.lower():
            continue                      # the declared outflow, asserted above
        if l_amount > 0:
            return Decision(False, (
                f"refused: the transaction emits a Transfer of {l_amount} from the "
                f"wallet on UNDECLARED token {l_token} — it moves an asset the "
                f"intent never mentioned"))

    if abs(deltas.native_delta) > _NATIVE_DUST_WEI:
        return Decision(False, (
            f"refused: unexpected native balance change of {deltas.native_delta} wei — "
            f"the transaction does something that was not declared"))

    # -- 7. Price the risk -------------------------------------------------
    amount_usd = None
    if intent.is_allowance_op:
        # The risk of an allowance op is the DECLARED GRANT, not the (zero)
        # outflow — pricing the outflow valued every approval at $0.00 and no
        # cap could bound it (§1.1, 2026-08-14). Every grant must carry a
        # trustworthy price: a low-confidence (thin/seedable-pool) token reads
        # None, and an unpriceable grant REFUSES rather than silently skipping
        # the USD bound — the owner can still approve it by hand.
        # A revoke declares no grant → $0 risk and needs no price at all, so
        # cleaning up a worthless/unpriceable token always stays possible.
        amount_usd = 0.0
        for (g_token, _g_spender, g_amount) in intent.expected_allowance_grants:
            try:
                unit_price = price_fn(intent.chain, g_token) if price_fn else None
            except Exception:
                unit_price = None
            if unit_price is None:
                # 028 (2026-08-22): a grant that cannot exceed what the wallet
                # ALREADY holds of g_token puts no NEW value at risk — the
                # high-confidence price bar exists to bound a grant that could
                # move more than intended, and an exit within held balance
                # structurally cannot. Still needs SOME price (fallback_price_fn,
                # unconstrained by the liquidity/pool-count floor) to compute
                # amount_usd for the cap checks below — an unpriceable-even-by-
                # fallback grant still refuses.
                exit_bounded = (
                    intent.held_balance_raw is not None
                    and g_token.lower() == (intent.token or "").lower()
                    and g_amount <= intent.held_balance_raw)
                if exit_bounded and fallback_price_fn:
                    try:
                        unit_price = fallback_price_fn(intent.chain, g_token)
                    except Exception:
                        unit_price = None
                    if unit_price is not None:
                        logger.info(
                            "tx_guard.exit_exemption chain=%s token=%s amount=%s "
                            "held_balance=%s — priced via fallback, high-confidence "
                            "price bar waived for an exit within held balance",
                            intent.chain, g_token, g_amount, intent.held_balance_raw)
                if unit_price is None:
                    if exit_bounded:
                        # 2026-08-26 exit untying: a grant that cannot exceed
                        # what is already held, on a token NO source can price,
                        # values at $0 instead of dead-ending. The refusal here
                        # left fired stop rules (BPAD, BaseUnc −54%) with no
                        # autonomous exit path at all; the swap that follows is
                        # valued exactly (measured quote inflow) and capped, and
                        # the grant is still simulation-asserted to the declared
                        # spender and amount.
                        logger.warning(
                            "tx_guard.exit_unpriceable_grant chain=%s token=%s "
                            "amount=%s held_balance=%s — no price by ANY source; "
                            "valuing the exit-bounded grant at $0 so the position "
                            "stays closable",
                            intent.chain, g_token, g_amount,
                            intent.held_balance_raw)
                        continue
                    return Decision(False, _unpriceable_reason(
                        "the allowance grant", exit_bounded=exit_bounded,
                        had_fallback=fallback_price_fn is not None))
            decimals = _decimals_for(intent.chain, g_token)
            if decimals is None:
                return Decision(False, (
                    "refused: token decimals unknown — cannot value the allowance grant"))
            amount_usd += (g_amount / (10 ** decimals)) * unit_price
    elif intent.token:
        try:
            unit_price = price_fn(intent.chain, intent.token) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            # 028 (2026-08-22): same exit exemption as the allowance-pricing
            # branch above — an outflow (e.g. a swap's token_in) that does not
            # exceed the wallet's OWN held balance of intent.token puts no NEW
            # value at risk beyond what is already owned; the swap's route
            # check and slippage bound already guard EXECUTION risk
            # separately. Still needs SOME price to compute the caps below.
            exit_bounded = (
                intent.held_balance_raw is not None
                and outflow_raw <= intent.held_balance_raw)
            if exit_bounded and fallback_price_fn:
                try:
                    unit_price = fallback_price_fn(intent.chain, intent.token)
                except Exception:
                    unit_price = None
                if unit_price is not None:
                    logger.info(
                        "tx_guard.exit_exemption chain=%s token=%s outflow=%s "
                        "held_balance=%s — priced via fallback, high-confidence "
                        "price bar waived for an exit within held balance",
                        intent.chain, intent.token, outflow_raw, intent.held_balance_raw)
            if unit_price is None and exit_bounded and intent.inflow_token:
                # 2026-08-26 exit untying: the sell of a held token that NO
                # source can price is valued at the simulation's MEASURED
                # inflow of the declared receive token. The receipt is exact
                # and unfalsifiable — a better cap number than any estimate —
                # and refusing here is what left fired stop rules (BPAD,
                # BaseUnc at −54%) permanently unexecutable. A missing or zero
                # measured inflow still refuses: unmeasured is never free.
                amount_usd = _inflow_valuation_usd(
                    intent, deltas, price_fn=price_fn,
                    fallback_price_fn=fallback_price_fn)
                if amount_usd is not None:
                    logger.info(
                        "tx_guard.exit_inflow_valuation chain=%s token_in=%s "
                        "inflow_token=%s amount_usd=%.4f — outflow unpriceable "
                        "by any source; caps run against the measured receipt",
                        intent.chain, intent.token, intent.inflow_token,
                        amount_usd)
            if unit_price is None and amount_usd is None:
                return Decision(False, _unpriceable_reason(
                    "the outflow", exit_bounded=exit_bounded,
                    had_fallback=fallback_price_fn is not None))
        if amount_usd is None:
            decimals = _decimals_for(intent.chain, intent.token)
            if decimals is None:
                return Decision(False, "refused: token decimals unknown — cannot value the outflow")
            amount_usd = (outflow_raw / (10 ** decimals)) * unit_price

    if amount_usd is None:
        return Decision(False, "refused: could not compute a USD value for this transaction")
    # Caps operate in CENTS (2026-08-26): a $1.9903-vs-$1.99 refusal is a
    # quote-rounding artifact, not a policy — it cost live runs a refuse/resize/
    # retry dance at every cap edge. Sub-cent drift carries no risk a cap can
    # meaningfully bound.
    amount_usd = round(amount_usd, 2)
    if monitor_exit and not intent.is_allowance_op:
        if not _measured_inflow_raw(intent, deltas):
            return Decision(False, (
                "refused: the monitor-exit lane requires a measured inflow of "
                "the declared receive token — the simulation shows none, so "
                "this is not an exit"))
    if amount_usd > intent.max_spend_usd:
        return Decision(False, (
            f"refused: ${amount_usd:.4f} exceeds the declared max_spend_usd "
            f"${intent.max_spend_usd:.4f}"))

    # -- 8. PolicyGate (per-tx ceiling, rolling caps, replay) --------------
    verdict = gate.check(venue="defi", amount_usd=amount_usd,
                         idempotency_key=intent.idempotency_key)
    if not verdict.allowed:
        return Decision(False, f"refused by PolicyGate: {verdict.reason}", amount_usd=amount_usd)

    # -- 9. Approval lane --------------------------------------------------
    if amount_usd > autonomous_max_usd():
        return Decision(False, (
            f"owner approval required: ${amount_usd:.4f} is above the autonomous "
            f"ceiling ${autonomous_max_usd():.2f}"),
            lane="owner_queue", amount_usd=amount_usd,
            sim_gas_used=deltas.gas_used)

    # An unattended, self-directed spend must have an aggregate damage bound. The
    # per-tx ceiling ($25 default) alone cannot stop an injection during a trade
    # leg from looping within-ceiling swaps that drain the treasury one ticket at
    # a time. WALLET_DAILY_CAP_USD is that bound; H3 (2026-08-22) gave it a
    # finite $100/24h default, so this refusal now only fires when the operator
    # has explicitly disabled the cap (WALLET_DAILY_CAP_USD=none/off) — arming
    # DEFI_AUTONOMOUS_TURN_TRADING on top of that would leave the loop unbounded.
    # Refuse the autonomous lane in that case; an owner-driven turn is unaffected
    # (autonomous_origin is False for a genuine owner turn).
    if autonomous_origin and not getattr(gate, "has_daily_cap", False):
        return Decision(False, (
            "refused: unattended trading needs an aggregate damage bound — set "
            "WALLET_DAILY_CAP_USD (the per-tx ceiling alone can be looped within "
            "to drain the treasury). Owner-driven trades are unaffected."),
            lane="owner_queue", amount_usd=amount_usd,
            sim_gas_used=deltas.gas_used)

    return Decision(True, "authorized", lane="autonomous", amount_usd=amount_usd,
                    sim_gas_used=deltas.gas_used)


def _measured_inflow_raw(intent: TxIntent, deltas) -> Optional[int]:
    """The simulation's measured POSITIVE delta of the declared inflow token."""
    if not intent.inflow_token:
        return None
    want = intent.inflow_token.lower()
    for token, moved in (deltas.token_deltas or {}).items():
        if token.lower() == want and moved > 0:
            return moved
    return None


def _inflow_valuation_usd(intent: TxIntent, deltas, *, price_fn,
                          fallback_price_fn) -> Optional[float]:
    """USD value of the measured receipt, or None when it cannot be computed.

    The chain's pinned USDC is $1.00 by definition — it is the unit the whole
    cap system is denominated in, and requiring a price FEED for it is how a
    feed outage once blocked exits outright. Any other inflow token (e.g. WETH
    on a chain with no pinned stablecoin) must be priced by a source.
    """
    inflow_raw = _measured_inflow_raw(intent, deltas)
    if not inflow_raw:
        return None
    decimals = _decimals_for(intent.chain, intent.inflow_token)
    if decimals is None:
        return None
    unit = None
    try:
        from core.wallet import chains
        row = chains.get(intent.chain)
        if row is not None and row.usdc and \
                intent.inflow_token.lower() == row.usdc.lower():
            unit = 1.0
    except Exception:
        unit = None
    if unit is None and price_fn:
        try:
            unit = price_fn(intent.chain, intent.inflow_token)
        except Exception:
            unit = None
    if unit is None and fallback_price_fn:
        try:
            unit = fallback_price_fn(intent.chain, intent.inflow_token)
        except Exception:
            unit = None
    if unit is None:
        return None
    return (inflow_raw / (10 ** decimals)) * unit


def _unpriceable_reason(what: str, *, exit_bounded: bool,
                        had_fallback: bool) -> str:
    """Why this could not be priced, and therefore what the caller can DO.

    029 R6. All three situations used to share one sentence ("no trustworthy
    price"), and the cost of that was concrete: the prod agent wrote a full
    design proposal for a deadlock that had already shipped as 028 and was live
    on the deployed SHA. It could not tell whether the exit exemption had failed,
    had never applied, or did not exist — so it assumed the last one.

    The gate is unchanged. Only the sentence differs, by which lever is missing.
    """
    head = f"refused: {what} has no trustworthy price, so no cap can bound "
    tail = ("what it puts at risk" if "allowance" in what else "it")
    if exit_bounded and had_fallback:
        return (head + tail + ". This IS an exit within your held balance, so the "
                "high-confidence price bar was already waived (028) — but the "
                "fallback pricer returned no price at all, so there is still no "
                "number to run the caps against. That is a price-source outage, "
                "not a policy refusal: retry when pricing recovers, or have the "
                "owner approve it by hand.")
    if exit_bounded and not had_fallback:
        return (head + tail + ". It is an exit within your held balance, which "
                "028 exempts from the high-confidence price bar — but no fallback "
                "pricer was wired into this call, so the exemption could not be "
                "applied. This is a wiring gap, not a decision about your trade.")
    return (head + tail + ". The exit exemption (028) did NOT apply here: it "
            "needs a declared held balance covering the amount, and this call "
            "declared none (or less than the amount), so the grant could put "
            "MORE at risk than you already own. Read your balance and declare it, "
            "or have the owner approve this by hand — an unpriceable token can be "
            "approved by the owner, not autonomously.")


def _decimals_for(chain: str, token: str) -> Optional[int]:
    try:
        from core.wallet.tokens import get_token_identity
        return get_token_identity(chain, token).decimals
    except Exception:
        return None
