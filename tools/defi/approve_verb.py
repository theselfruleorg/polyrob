"""``defi_trade.approve_token`` / ``revoke_approval`` — exact-amount allowance hygiene.

An approval is a standing claim on the wallet: ``approve_token`` grants an EXACT
amount (unlimited is refused) and ``revoke_approval`` sets it back to zero.

Moved out of ``trade_tool.py`` (W9 god-file split, behaviour-preserving). The
tool keeps thin delegating methods; module-level helpers are imported from
``tools.defi.trade_tool`` at call time so a test that patches them there still
reaches this code.
"""
from __future__ import annotations



def _autonomous_run(context, tool_self=None) -> bool:
    """The turn is a goal/cron/planner run (``core.security.owner_turn``). Raises on a
    probe fault, so the kernel's turn step refuses (fail closed)."""
    from core.security.owner_turn import is_autonomous_session
    return is_autonomous_session(context, tool_self)


def _turn_line(ctx) -> str:
    """Who asked, for the card body (audit 2026-11-21 L-1): the turn's origin and its
    session, so the owner sees which run wants the allowance."""
    from core.security.owner_turn import forged_kind, is_room_turn
    kind = forged_kind(ctx)
    origin = (f"a {kind.replace('_', '-')} re-entry" if kind
              else "a room turn" if is_room_turn(ctx) else "an agent turn")
    sid = str(getattr(ctx, "session_id", "") or "unknown")
    return f"Asked by the agent on {origin} (session {sid}), not by you."


async def _grant_card(tool, params, ctx, spender: str, token: str):
    """126: a grant to an unpinned spender is the OWNER's act (CR-H02). When the
    caller is the owner tenant on an agent turn that cannot make it, the exact grant
    becomes ONE call card; its Confirm runs it as the owner seat, where CR-H02 passes
    and every other gate runs again. Anyone else: None (the flat refusal).

    Audit 2026-11-21 L-1: an AUTONOMOUS run (goal, cron, planner) gets no card — a
    standing claim is never one tap away from an unattended run — and the card names
    the turn's origin and session. The decision is the money kernel's
    (``authorize_spend``: principal, leaf, and the turn step with the autonomous-run
    probe; a probe fault refuses)."""
    if ctx is None:
        return None
    try:
        from core.money.authorize import SpendIntent, authorize_spend
        verdict = authorize_spend(
            SpendIntent.verb_gate("grant an allowance to an unpinned spender", turn=True),
            ctx, forged_fn=_autonomous_run, tool_self=tool)
        if verdict.refused:
            return None
        who = _turn_line(ctx)
    except Exception:  # noqa: BLE001
        return None
    from tools.controller.call_card_run import need_owner
    args = {k: v for k, v in params.model_dump().items() if v is not None}
    args["dry_run"] = False
    body = (f"Approve {params.amount:g} of {token} for {spender} on {params.chain}, bound "
            f"${params.max_spend_usd:,.2f}.\n{who}\n⚠️ {spender} is not a swap router this "
            f"chain pins: the allowance is a standing claim on the wallet until it is revoked "
            f"(revoke_approval). Its value is charged to the daily cap.")
    return await need_owner(tool, ctx, title="Approve a token for a spender", body=body,
                            steps=[{"do": "defi_trade_approve_token", "args": args}],
                            lead="An allowance to an unpinned spender is the owner's act.")


async def perform_approve_token(tool, params, execution_context=None):
    from tools.defi.trade_tool import (  # call-time: tests patch these
        _SYMBOL_NOTE, _UNLIMITED_APPROVAL_FLOOR, _intent_idem, _shown_symbol,
        _unsupported_chain)
    if getattr(params, "via", "erc20") == "permit2":
        from tools.defi.lp_v4_verbs import perform_permit2_approve
        return await perform_permit2_approve(tool, params, execution_context)
    from core.wallet import tx_guard
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.tokens import get_token_identity, normalize_address
    from tools.defi.providers import univ3

    chain_err = _unsupported_chain(params.chain)
    if chain_err:
        return tool._ar(error=chain_err)
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)
    try:
        token = normalize_address(params.token)
        spender = normalize_address(params.spender)
    except ValueError as exc:
        return tool._ar(error=str(exc))

    ident = get_token_identity(params.chain, token)
    if ident.decimals is None:
        return tool._ar(error=(
            f"{token} does not report decimals — refusing to compute an "
            f"allowance for a token whose denomination is unknown"))
    amount_raw = int(round(params.amount * (10 ** ident.decimals)))
    if amount_raw >= _UNLIMITED_APPROVAL_FLOOR:
        return tool._ar(error=(
            "refused: that is an effectively UNLIMITED approval. An unlimited "
            "grant outlives the trade and remains a standing claim on the "
            "wallet. Approve the exact amount this trade needs."))

    # The infinite marker alone is not enough: 1e30 USDC is absurd yet sits
    # far below 2**128. Bound the approval by the USD the caller declared —
    # an allowance is a claim on funds, so it belongs under the same ceiling
    # as a spend.
    unit_price = tool._price(params.chain, token)
    if unit_price:
        # Cents, not sub-cent noise (2026-08-26): $1.9906 at-risk against a
        # declared $1.99 was a live refuse/resize/retry dance at every cap
        # edge. Sub-cent drift is quote rounding, not risk.
        approval_usd = round(params.amount * unit_price, 2)
        if approval_usd > params.max_spend_usd:
            return tool._ar(error=(
                f"refused: this approval puts ${approval_usd:,.2f} at risk but "
                f"you declared max_spend_usd=${params.max_spend_usd:,.2f}. "
                f"Approve only what the trade needs."))
    elif spender.lower() not in tx_guard.pinned_route_spenders(params.chain):
        # S3 (2026-09-14): the bound above sat INSIDE `if unit_price:`, so a
        # token no source can price — a just-launched coin has no pair —
        # carried no bound at all here, and the guard's exit exemption then
        # valued it at $0.00. An unvalued grant must not read as free.
        #
        # The one grant on an unpriceable token that still makes sense is
        # the SELL LEG, and the sell leg approves the chain's pinned router.
        # Anything else is refused here with a remedy rather than at the
        # guard with a message about a check the caller never heard of.
        return tool._ar(error=(
            f"refused: no trustworthy price for {token}, so nothing can bound "
            f"what this allowance puts at risk — and {spender} is not a swap "
            f"router this chain pins, so the grant is not the sell leg of an "
            f"exit either. If you are closing a position, approve the router "
            f"the swap route names. No seat and no card can make this grant: "
            f"nothing bounds it. Use revoke_approval to clear a standing claim."))

    # CR-H02 (2026-09-23): a grant to a spender this chain does not pin is
    # not the approve leg of a swap — it is a standing claim whose drain
    # (the spender's transferFrom) no guard ever sees. Only a genuine owner
    # turn may create one, and when it does the grant's USD is CHARGED to
    # the rolling cap (below, via charge_grant) instead of booking $0.
    spender_pinned = spender.lower() in tx_guard.pinned_route_spenders(params.chain)
    if not spender_pinned:
        refusal = tool._non_route_grant_refusal(execution_context, spender)
        if refusal:
            card = (None if "could not be proven" in refusal else
                    await _grant_card(tool, params, execution_context, spender, token))
            if card is not None and not card.error:
                return card
            return tool._ar(error=refusal)

    signer = wallet.operational_signer()
    gate = wallet.policy
    # 028 (2026-08-22): the wallet's OWN held balance of `token`, so the
    # guard can waive the high-confidence price bar for an exit that
    # cannot exceed what is already owned. A read failure is None — no
    # exemption, same refusal as before 028 (fail closed, not open).
    try:
        held_balance_raw = tool._held_balance_raw(params.chain, signer.address, token)
    except Exception:
        held_balance_raw = None

    # 068 G4: the same full-exit clamp swap applies. On 2026-09-25 the owner
    # said "sell it" and approve was refused twice: the amount came from a
    # 6-decimal display figure rounded UP, 1.4e-7 token above the held
    # balance, so the 028 exit exemption (grant <= held) did not apply.
    if (held_balance_raw is not None and held_balance_raw > 0
            and held_balance_raw < amount_raw <= int(held_balance_raw * 1.01)):
        amount_raw = held_balance_raw

    data = univ3.build_approve_data(spender=spender, amount_raw=amount_raw)
    rail = (tool._rail_factory or EvmRail)(chain=params.chain, signer=signer)
    try:
        tx = rail.build_call(to=token, data=data, value=0)
    except Exception as exc:
        return tool._ar(error=f"could not build the transaction: {exc}")
    idem = _intent_idem("approve", params.chain, tx, execution_context)

    # DECLARING the grant is what lets the guard verify it: an allowance the
    # simulation reveals but the intent did not declare is refused.
    intent = tx_guard.TxIntent(
        chain=params.chain, token=token, to=spender, amount_raw=0,
        max_spend_usd=params.max_spend_usd,
        expected_allowance_grants=((token, spender, amount_raw),),
        is_allowance_op=True, idempotency_key=idem,
        held_balance_raw=held_balance_raw)
    header = (f"approve {params.amount} {_shown_symbol(ident.symbol, token)} for {spender}\n"
              + (_SYMBOL_NOTE if ident.symbol else ""))
    async with gate.reserve():
        return await tool._run_guarded(
            intent=intent, tx=tx, rail=rail, gate=gate, signer=signer,
            execution_context=execution_context, header=header,
            dry_run=params.dry_run, venue_action="approve", idem=idem,
            counterparty=spender, charge_grant=not spender_pinned)


async def perform_revoke_approval(tool, params, execution_context=None):
    from tools.defi.trade_tool import (  # call-time: tests patch these
        _SYMBOL_NOTE, _intent_idem, _shown_symbol, _unsupported_chain)
    from core.wallet import tx_guard
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.tokens import get_token_identity, normalize_address
    from tools.defi.providers import univ3

    chain_err = _unsupported_chain(params.chain)
    if chain_err:
        return tool._ar(error=chain_err)
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)
    try:
        token = normalize_address(params.token)
        spender = normalize_address(params.spender)
    except ValueError as exc:
        return tool._ar(error=str(exc))

    ident = get_token_identity(params.chain, token)
    signer = wallet.operational_signer()
    gate = wallet.policy
    data = univ3.build_approve_data(spender=spender, amount_raw=0)
    rail = (tool._rail_factory or EvmRail)(chain=params.chain, signer=signer)
    try:
        tx = rail.build_call(to=token, data=data, value=0)
    except Exception as exc:
        return tool._ar(error=f"could not build the transaction: {exc}")
    idem = _intent_idem("revoke", params.chain, tx, execution_context)

    # A revoke grants nothing, so it declares no allowance and needs no USD
    # headroom; the guard still simulates it and refuses any hidden grant.
    intent = tx_guard.TxIntent(
        chain=params.chain, token=token, to=spender, amount_raw=0,
        max_spend_usd=0.01, expected_allowance_grants=(),
        is_allowance_op=True, idempotency_key=idem)
    header = (f"revoke {_shown_symbol(ident.symbol, token)} allowance for {spender}\n"
              + (_SYMBOL_NOTE if ident.symbol else ""))
    async with gate.reserve():
        return await tool._run_guarded(
            intent=intent, tx=tx, rail=rail, gate=gate, signer=signer,
            execution_context=execution_context, header=header,
            dry_run=params.dry_run, venue_action="revoke", idem=idem,
            counterparty=spender)
