"""tx_guard steps 7-9: price the risk, the PolicyGate caps, the approval lane, the final allow.

Part of the money authorizer split (W6): every function here is one step of
``core.wallet.tx_guard._authorize``, moved VERBATIM. A step reads what it needs
from the :class:`~core.wallet.tx_guard_steps.context.GuardContext`, returns a
refusing :class:`~core.wallet.tx_guard.Decision` to stop the guard, or writes
what later steps read back to the context and returns ``None``. Exceptions are
not caught here: a step raises exactly where the single function raised.

⚠️ Module-level helpers are reached as ``_tg.<name>`` on purpose — callers and
tests monkeypatch them on ``core.wallet.tx_guard``, and a direct import would
bind the original. The step ORDER lives in ``tx_guard_steps.STEPS`` only.
"""
import math
from typing import Optional

from core.wallet import tx_guard as _tg
from core.wallet.tx_guard import Decision
from core.wallet.tx_guard_steps.context import GuardContext


def step_7_price(c: GuardContext) -> Optional[Decision]:
    """7. Price the risk."""
    intent = c.intent
    execution_context = c.execution_context
    price_fn = c.price_fn
    fallback_price_fn = c.fallback_price_fn
    monitor_exit = c.monitor_exit
    outer_tx = c.outer_tx
    deltas = c.deltas
    lp_implied = c.lp_implied
    outflow_raw = c.outflow_raw
    # -- 7. Price the risk -------------------------------------------------
    amount_usd = None
    valuation_basis = "outflow"
    if intent.is_collection_mint:
        # A paid mint's cost is its VALUE PLUS ITS FEE (the deploy precedent): the price
        # leaves for good, and the tokens that arrive have no market price to net against
        # it (an NFT is unpriceable, see below). The worst-case fee is what size_gas signs for.
        from core.wallet import chains as _chains
        _row = _chains.get(intent.chain)
        _proxy = getattr(_row, "wrapped_native", None) if _row else None
        if not _proxy:
            return Decision(False, (
                f"refused: chain {intent.chain!r} pins no wrapped-native address, so the "
                f"mint price cannot be priced — and an unpriced spend booked at $0.00 would "
                f"widen every other cap by its own size"))
        try:
            unit_price = price_fn(intent.chain, _proxy) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            return Decision(False, _tg._unpriceable_reason(
                "the mint price", exit_bounded=False,
                had_fallback=fallback_price_fn is not None))
        _sized_gas = max(int((deltas.gas_used or 0) * 3 // 2), int(outer_tx.get("gas") or 0))
        _worst_fee_wei = _sized_gas * int(outer_tx.get("maxFeePerGas") or 0)
        _decimals = int(getattr(_row, "native_decimals", 18) or 18)
        amount_usd = ((outflow_raw + _worst_fee_wei) / (10 ** _decimals)) * unit_price
    elif (intent.is_claim or intent.is_nft_op or intent.is_registration or intent.is_collection_reveal
            or intent.is_journal_entry
            or (intent.is_liquidity_op and not intent.lp_outflows)):
        # The cost of a claim is its FEE. Nothing leaves, so pricing an outflow
        # would book it at $0.00 — the confident-zero class that let 114
        # tenantless wallet_spend rows read as a treasury that had spent
        # nothing. The RECEIPT is emphatically not the cost: charging 13.6 ETH
        # arriving against the spend caps would refuse the agent its own money.
        #
        # An NFT op prices the SAME way, for a different reason: a collectible
        # is genuinely unpriceable, and inventing a floor would make the cap
        # lie about the one asset class that is easiest to wash-trade. The fee
        # is the only figure here that is true. What actually bounds an NFT
        # move is the owner (spend_lane.ALWAYS_OWNER_APPROVED_VERBS), not a cap.
        _what = "the claim fee" if intent.is_claim else "the transaction fee"
        # A registration's whole cost is its fee: it sends nothing and receives
        # an identity that has no market price to book against a cap.
        from core.wallet import chains as _chains
        _row = _chains.get(intent.chain)
        _proxy = getattr(_row, "wrapped_native", None) if _row else None
        if not _proxy:
            return Decision(False, (
                f"refused: chain {intent.chain!r} pins no wrapped-native address, "
                f"so {_what} cannot be priced — and an unpriced spend "
                f"booked at $0.00 would widen every other cap by its own size"))
        try:
            unit_price = price_fn(intent.chain, _proxy) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            return Decision(False, _tg._unpriceable_reason(
                _what, exit_bounded=False,
                had_fallback=fallback_price_fn is not None))
        _sized_gas = max(int((deltas.gas_used or 0) * 3 // 2), int(outer_tx.get("gas") or 0))
        _worst_fee_wei = _sized_gas * int(outer_tx.get("maxFeePerGas") or 0)
        _decimals = int(getattr(_row, "native_decimals", 18) or 18)
        amount_usd = (_worst_fee_wei / (10 ** _decimals)) * unit_price
    elif intent.is_liquidity_op:
        from core.wallet import liquidity_guard
        try:
            amount_usd, lp_implied = liquidity_guard.price_outflows(
                intent, deltas, price_fn, fallback_price_fn, _tg._decimals_for)
        except (ValueError, TypeError) as exc:
            return Decision(False, f"refused: {exc}")
    elif intent.is_allowance_op:
        # The risk of an allowance op is the DECLARED GRANT, not the (zero)
        # outflow — pricing the outflow valued every approval at $0.00 and no
        # cap could bound it (§1.1, 2026-08-14).
        # A revoke declares no grant → $0 risk and needs no price at all, so
        # cleaning up a worthless/unpriceable token always stays possible.
        amount_usd, refusal = _tg._price_allowance_grants(
            intent, price_fn=price_fn, fallback_price_fn=fallback_price_fn,
            execution_context=execution_context)
        if refusal is not None:
            return refusal
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
                    _tg.logger.info(
                            "tx_guard.exit_exemption chain=%s token=%s outflow=%s "
                            "held_balance=%s — priced via fallback, high-confidence "
                            "price bar waived for an exit within held balance",
                            intent.chain, intent.token, outflow_raw, intent.held_balance_raw)
            if unit_price is None and intent.min_native_inflow_wei is not None:
                # 042: the NATIVE twin of the exit-untying valuation below. A
                # launchpad token minutes old has no price at ANY source, so
                # pricing the outflow dead-ends and the sell refuses — the
                # reachable-but-not-executable failure that left fired stop
                # rules unexecutable in August, reproduced on a new rail.
                #
                # The measured native RECEIPT is exact and unfalsifiable — a
                # better cap number than any estimate of the token — and it was
                # already asserted against the declared minimum in step 6b. An
                # unpriceable native asset still refuses; so does a zero
                # receipt, because unmeasured is never free.
                amount_usd = _tg._native_inflow_valuation_usd(
                    intent, deltas, price_fn=price_fn)
                if amount_usd is not None:
                    valuation_basis = "inflow"
                    _tg.logger.info(
                            "tx_guard.native_inflow_valuation chain=%s token_in=%s "
                            "amount_usd=%.4f — outflow unpriceable; caps run "
                            "against the measured native receipt",
                            intent.chain, intent.token, amount_usd)
            if unit_price is None and amount_usd is None and exit_bounded and intent.inflow_token:
                # 2026-08-26 exit untying: the sell of a held token that NO
                # source can price is valued at the simulation's MEASURED
                # inflow of the declared receive token. The receipt is exact
                # and unfalsifiable — a better cap number than any estimate —
                # and refusing here is what left fired stop rules (BPAD,
                # BaseUnc at −54%) permanently unexecutable. A missing or zero
                # measured inflow still refuses: unmeasured is never free.
                amount_usd = _tg._inflow_valuation_usd(
                    intent, deltas, price_fn=price_fn,
                    fallback_price_fn=fallback_price_fn)
                if amount_usd is not None:
                    valuation_basis = "inflow"
                    _tg.logger.info(
                            "tx_guard.exit_inflow_valuation chain=%s token_in=%s "
                            "inflow_token=%s amount_usd=%.4f — outflow unpriceable "
                            "by any source; caps run against the measured receipt",
                            intent.chain, intent.token, intent.inflow_token,
                            amount_usd)
            if unit_price is None and amount_usd is None:
                return Decision(False, _tg._unpriceable_reason(
                    "the outflow", exit_bounded=exit_bounded,
                    had_fallback=fallback_price_fn is not None))
        if amount_usd is None:
            decimals = _tg._decimals_for(intent.chain, intent.token)
            if decimals is None:
                return Decision(False, "refused: token decimals unknown — cannot value the outflow")
            amount_usd = (outflow_raw / (10 ** decimals)) * unit_price
    elif intent.is_deploy:
        # A deployment's cost is its ENDOWMENT PLUS ITS FEE, and the fee is
        # usually the whole of it — a token deploy endows nothing and still
        # burns 500k gas. Pricing only the (zero) endowment would book every
        # deployment at $0.00, which is the confident-zero class that let 114
        # tenantless wallet_spend rows read as a treasury that had spent nothing.
        # The worst case is what `size_gas` is about to sign for, so that is what
        # the caps are charged.
        from core.wallet import chains as _chains
        _row = _chains.get(intent.chain)
        _proxy = getattr(_row, "wrapped_native", None) if _row else None
        if not _proxy:
            return Decision(False, (
                f"refused: chain {intent.chain!r} pins no wrapped-native address, "
                f"so a deployment's fee cannot be priced — and an unpriced spend "
                f"booked at $0.00 would widen every other cap by its own size"))
        try:
            unit_price = price_fn(intent.chain, _proxy) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            return Decision(False, _tg._unpriceable_reason(
                "the deployment fee", exit_bounded=False,
                had_fallback=fallback_price_fn is not None))
        _sized_gas = max(int((deltas.gas_used or 0) * 3 // 2), int(outer_tx.get("gas") or 0))
        _worst_fee_wei = _sized_gas * int(outer_tx.get("maxFeePerGas") or 0)
        _decimals = int(getattr(_row, "native_decimals", 18) or 18)
        amount_usd = ((outflow_raw + _worst_fee_wei) / (10 ** _decimals)) * unit_price
    else:
        # NATIVE outflow (039 B1). The chain's WRAPPED native is the pricing
        # proxy — the same asset at the same number — and it is already pinned in
        # the chain registry, so this adds no new price source and no new address
        # to verify. A chain that pins none cannot be priced here and REFUSES:
        # booking an unpriced outflow at $0.00 would widen every other money
        # verb's remaining cap by the size of this transaction, which is the
        # confident-zero class the 114 tenantless wallet_spend rows came from.
        from core.wallet import chains as _chains
        _row = _chains.get(intent.chain)
        _proxy = getattr(_row, "wrapped_native", None) if _row else None
        if not _proxy:
            return Decision(False, (
                f"refused: chain {intent.chain!r} pins no wrapped-native address, so a "
                f"native outflow cannot be priced — and an unpriced outflow booked at "
                f"$0.00 would widen every other cap by its own size"))
        try:
            unit_price = price_fn(intent.chain, _proxy) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            return Decision(False, _tg._unpriceable_reason(
                "the native outflow", exit_bounded=False,
                had_fallback=fallback_price_fn is not None))
        _decimals = int(getattr(_row, "native_decimals", 18) or 18)
        amount_usd = (outflow_raw / (10 ** _decimals)) * unit_price

    # A DECLARED grant is priced whatever shape the transaction is (S4,
    # 2026-09-14). The loop above only ran under `is_allowance_op`, so
    # `defi_trade.call` — which declares `allow_spender`/`allow_max_raw` and is
    # NOT an allowance op — had its grant valued at nothing: the outflow alone
    # carried the cap, and the standing claim the call left behind was free. A
    # claim on funds belongs under the same ceiling as a spend, in every verb
    # that can create one.
    if intent.expected_allowance_grants and not intent.is_allowance_op:
        grants_usd, refusal = _tg._price_allowance_grants(
            intent, price_fn=price_fn, fallback_price_fn=fallback_price_fn,
            execution_context=execution_context)
        if refusal is not None:
            return refusal
        amount_usd = (amount_usd or 0.0) + grants_usd

    if amount_usd is None:
        return Decision(False, "refused: could not compute a USD value for this transaction")
    fee_already_priced = (intent.is_collection_mint or intent.is_deploy or intent.is_claim
                         or intent.is_nft_op or intent.is_registration
                         or intent.is_collection_reveal or intent.is_journal_entry
                         or (intent.is_liquidity_op and not intent.lp_outflows))
    # The declared max_spend_usd asserts the transaction's VALUE (what it moves
    # or grants); the network fee added below is charged to the PolicyGate caps
    # (WAL-6) only. Charging the fee to the declared max refused every revoke on
    # its own gas (prod 2026-10-08) and every transfer/wrap/swap declared at
    # exactly its value. A shape whose fee IS its value (deploy, claim, mint,
    # fee-only LP) keeps the fee in the declared check.
    value_usd = amount_usd
    if not fee_already_priced:
        from core.wallet import chains as _chains
        row = _chains.get(intent.chain)
        gas = max(int(outer_tx.get("gas") or 0), int((deltas.gas_used or 0) * 3 // 2))
        fee_wei = gas * int(outer_tx.get("maxFeePerGas") or 0)
        if fee_wei:
            try:
                native_price = price_fn(intent.chain, row.wrapped_native) if row and price_fn else None
                if (native_price is None or not math.isfinite(native_price) or native_price < 0
                        or (native_price == 0 and not row.valueless)):
                    raise ValueError("missing native price")
                amount_usd += fee_wei / (10 ** row.native_decimals) * native_price
            except Exception:
                return Decision(False, "refused: cannot price the transaction fee")
    if not math.isfinite(amount_usd) or amount_usd < 0:
        return Decision(False, "refused: transaction valuation is not finite and non-negative")
    # Conservatively round up: repeated sub-cent spends must consume budget.
    if intent.is_liquidity_op and not intent.lp_outflows:
        if amount_usd <= 0:
            return Decision(False, "refused: liquidity fee was not measured/priced")
        amount_usd = max(0.01, amount_usd)
    from core.money.valuation import usd_ceiling
    amount_usd = usd_ceiling(amount_usd)
    if monitor_exit and not intent.is_allowance_op and not intent.is_liquidity_op:
        if not _tg._measured_inflow_raw(intent, deltas):
            return Decision(False, (
                "refused: the monitor-exit lane requires a measured inflow of "
                "the declared receive token — the simulation shows none, so "
                "this is not an exit"))
    _declared_usd = amount_usd if fee_already_priced else usd_ceiling(value_usd)
    if _declared_usd > intent.max_spend_usd:
        return Decision(False, (
            f"refused: ${_declared_usd:.4f} exceeds the declared max_spend_usd "
            f"${intent.max_spend_usd:.4f}"))
    c.lp_implied = lp_implied
    c.amount_usd = amount_usd
    c.valuation_basis = valuation_basis
    return None


def step_8_policy_gate(c: GuardContext) -> Optional[Decision]:
    """8. PolicyGate (per-tx ceiling, rolling caps, replay)."""
    intent = c.intent
    gate = c.gate
    execution_context = c.execution_context
    tool_self = c.tool_self
    pause_fn = c.pause_fn
    amount_usd = c.amount_usd
    # -- 8. PolicyGate (per-tx ceiling, rolling caps, replay) --------------
    from core.money.ledger import pause_probe
    with pause_probe(pause_fn):
        verdict = gate.check(venue="defi", amount_usd=amount_usd,
                             idempotency_key=intent.idempotency_key)
    if not verdict.allowed:
        from core.security.refusals import record_refusal
        record_refusal("money_gate", tool=getattr(tool_self, "name", None) or "",
                       user_id=getattr(execution_context, "user_id", None) or "",
                       detail=verdict.reason)
        return Decision(False, f"refused by PolicyGate: {verdict.reason}", amount_usd=amount_usd,
                        cap_exceeded=bool(getattr(verdict, "cap_exceeded", False)))
    return None


def step_9_approval_lane(c: GuardContext) -> Optional[Decision]:
    """9. Approval lane."""
    gate = c.gate
    execution_context = c.execution_context
    owner_direct = c.owner_direct
    owner_granted = c.owner_granted
    autonomous_origin = c.autonomous_origin
    deltas = c.deltas
    deploy_facts = c.deploy_facts
    position_token_id = c.position_token_id
    registered_agent_id = c.registered_agent_id
    amount_usd = c.amount_usd
    # -- 9. Approval lane --------------------------------------------------
    # The ceiling is the agent's own dial ("ask me above $X"). An owner-direct
    # turn already IS the owner asking — a model-built spend above the ceiling
    # was tapped by the controller's pre-hook before it got here (a declared
    # bound within the ceiling is the only exemption, and step 7 holds the
    # simulated value to it). An owner grant for THIS call is the tap this lane
    # exists to collect; before 2026-09-26 the grant was consumed and this step
    # re-queued the call: "NOT SENT".
    _ceiling = _tg.autonomous_max_usd(*_tg.ceiling_scope(execution_context))
    if amount_usd > _ceiling and not owner_direct and not owner_granted:
        return Decision(False, (
            f"owner approval required: ${amount_usd:.4f} is above the autonomous "
            f"ceiling ${_ceiling:.2f}"),
            lane="owner_queue", amount_usd=amount_usd,
            sim_gas_used=deltas.gas_used, deploy=deploy_facts,
            agent_id=registered_agent_id, position_token_id=position_token_id)

    # An unattended, self-directed spend must have an aggregate damage bound. The
    # autonomous ceiling (DEFI_AUTONOMOUS_MAX_USD, $25 default) alone cannot stop
    # an injection during a trade leg from looping within-ceiling swaps that
    # drain the treasury one ticket at a time. WALLET_DAILY_CAP_USD is that bound; H3 (2026-08-22) gave it a
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
            sim_gas_used=deltas.gas_used, deploy=deploy_facts,
            agent_id=registered_agent_id, position_token_id=position_token_id)
    return None


def allow(c: GuardContext) -> Optional[Decision]:
    """The final allow."""
    owner_direct = c.owner_direct
    owner_granted = c.owner_granted
    deltas = c.deltas
    deploy_facts = c.deploy_facts
    lp_implied = c.lp_implied
    position_token_id = c.position_token_id
    registered_agent_id = c.registered_agent_id
    amount_usd = c.amount_usd
    valuation_basis = c.valuation_basis
    _lane = ("owner_direct" if owner_direct
             else "owner_approved" if owner_granted else "autonomous")
    return Decision(True, "authorized" + (" (paired-leg implied valuation)" if lp_implied else ""), lane=_lane, amount_usd=amount_usd,
                    sim_gas_used=deltas.gas_used, deploy=deploy_facts,
                    agent_id=registered_agent_id, position_token_id=position_token_id,
                    simulated_token_deltas=dict(deltas.token_deltas),
                    valuation_basis=valuation_basis)
