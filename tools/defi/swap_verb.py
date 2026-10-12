"""``defi_trade.swap`` — the EVM swap through the route provider seam.

Moved out of ``trade_tool.py`` (W9 god-file split, behaviour-preserving). The
tool keeps a thin delegating method; module-level helpers are imported from
``tools.defi.trade_tool`` at call time so a test that patches them there still
reaches this code.
"""
from __future__ import annotations

import time


async def perform_swap(tool, params, execution_context=None):
    from tools.defi.trade_tool import (  # call-time: tests patch these
        PRECONDITION, _QUOTE_MAX_AGE_SEC, _SYMBOL_NOTE, _intent_idem,
        _max_slippage_bps, _no_swap_route, _route_drift_max_pct, _shown_symbol,
        _unsupported_chain)
    from core.wallet import tx_guard
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.tokens import get_token_identity, normalize_address

    # The chain refusal comes FIRST: "solana is read-only here" is true
    # whatever the wallet's state, while "agent wallet not enabled" implies
    # enabling it would help, which on a read-only chain it would not.
    chain_err = _unsupported_chain(params.chain) or _no_swap_route(params.chain)
    if chain_err:
        return tool._ar(error=chain_err)
    wallet = tool._get_wallet()
    if wallet is None:
        return tool._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
    from core.wallet import chains as _chains_mod
    from tools.defi.providers import routes as _routes
    try:
        # NATIVE-in is the sentinel word, never an address, so it must not
        # be run through the address normalizer.
        native_in = _routes.is_native(params.token_in)
        token_in = (_routes.NATIVE if native_in
                    else normalize_address(params.token_in))
        token_out = normalize_address(params.token_out)
    except ValueError as exc:
        return tool._ar(error=str(exc))
    if token_in == token_out:
        return tool._ar(error="token_in and token_out are the same token")

    id_out = get_token_identity(params.chain, token_out)
    from tools.defi.buy_screen import evm_buy_refusal, unchecked_route_refusal
    if not params.dry_run:
        if why := await evm_buy_refusal(params.chain, token_out, id_out):
            return tool._ar(error=why)
    if native_in:
        # The native asset has no contract to ask, and every EVM chain this
        # rail supports denominates it in 18 decimals (the ChainRow records
        # the SYMBOL for display; there is no non-18 native among them).
        id_in = None
        in_decimals = 18
        in_label = getattr(_chains_mod.get(params.chain), "native_symbol", "native")
    else:
        id_in = get_token_identity(params.chain, token_in)
        if id_in.decimals is None:
            return tool._ar(error=(
                "one side does not report decimals — refusing to size a swap "
                "against a token whose denomination is unknown"))
        in_decimals = id_in.decimals
        in_label = id_in.symbol or token_in
    if id_out.decimals is None:
        return tool._ar(error=(
            "one side does not report decimals — refusing to size a swap "
            "against a token whose denomination is unknown"))
    amount_in_raw = int(round(params.amount_in * (10 ** in_decimals)))

    signer = wallet.operational_signer()
    gate = wallet.policy
    slippage = params.slippage_bps or _max_slippage_bps()
    # 069 v4 A3: `account=` / `nft=` — the swap spends and receives in the NFT's account;
    # the treasury signs as the NFT's owner. The route pays the ACCOUNT.
    from tools.defi import account_mode
    held = None
    if account_mode.requested(params):
        held, why = account_mode.resolve(params, params.chain, signer.address,
                                         rpc=tool._account_rpc)
        if why:
            return tool._ar(error=why)
    holder_addr = held.account if held is not None else signer.address

    # 028 (2026-08-22): the wallet's OWN held balance of token_in, so the
    # guard can waive the high-confidence price bar for a sell that
    # cannot exceed what is already owned. A read failure is None — no
    # exemption, same refusal as before 028 (fail closed, not open).
    # Read BEFORE routing (2026-08-26) so a full exit can clamp below.
    try:
        # Native has no ERC-20 balance to read. None = "unknown", which is
        # the fail-CLOSED value here: it withholds the 028 price-bar
        # exemption and the full-exit clamp rather than granting either on a
        # number that was never measured.
        held_balance_raw = (
            None if native_in
            else tool._held_balance_raw(params.chain, holder_addr, token_in))
    except Exception:
        held_balance_raw = None

    # Full-exit clamp (2026-08-26): a declared amount within 1% ABOVE the
    # held balance is a rounding overshoot, not a wrong size — the live
    # case was selling 28.925134 against a 28.92513399… balance, refused
    # by the token contract (STF) and retried by hand at lower precision.
    # A real overshoot (>1%) keeps the declaration and fails honestly.
    if (held_balance_raw is not None and held_balance_raw > 0
            and held_balance_raw < amount_in_raw <= int(held_balance_raw * 1.01)):
        amount_in_raw = held_balance_raw

    # The recipient is baked into the calldata, so the route must be built
    # for THIS signer — a route quoted for another address would send the
    # output somewhere else.
    route, route_why = tool._route(params.chain, token_in, token_out,
                                   amount_in_raw, holder=holder_addr,
                                   slippage_bps=slippage)
    if route is None:
        return tool._ar(error=(
            f"cannot route {in_label if (native_in or not id_in.symbol) else _shown_symbol(id_in.symbol, token_in)} -> "
            f"{_shown_symbol(id_out.symbol, token_out)}: {route_why}"),
            error_kind=PRECONDITION)

    # Freshness window (§1.3): quoted_at is enforced, not decorative. An
    # aggregator quote is MORE perishable than a pool read, not less.
    age = time.time() - route.quoted_at
    if age > _QUOTE_MAX_AGE_SEC:
        return tool._ar(error=(
            f"refused: the quote is stale ({age:.0f}s old, max "
            f"{_QUOTE_MAX_AGE_SEC:.0f}s) — prices move; re-quote and execute "
            f"promptly. Nothing was broadcast."), error_kind=PRECONDITION)

    amount_out_min = route.amount_out_min_raw
    if not amount_out_min or amount_out_min <= 0:
        return tool._ar(error="the route carries no minimum output — refused")

    if params.minimum_output_raw is not None and amount_out_min < params.minimum_output_raw:
        return tool._ar(error="The minimum received fell below the confirmed quote; nothing was built")

    from tools.defi.identity_gate import trusted_buy
    out_trusted = trusted_buy(id_out, chain=params.chain, token_out=token_out,
                              execution_context=execution_context)
    sanity_verdict, sanity_note = tool._route_sanity(params.chain, route,
                                                     id_in, id_out,
                                                     slippage_bps=slippage,
                                                     held_balance_raw=held_balance_raw,
                                                     out_trusted=out_trusted)
    # Dry run included: a dry run that reports "authorized" for a send this
    # check will refuse told the 2026-10-10 buyback to go, then refused it.
    if why := unchecked_route_refusal(sanity_verdict, params.max_spend_usd,
                                      trusted=out_trusted):
        return tool._ar(error=why if not params.dry_run
                        else f"dry run — the live send would be {why}")
    # 068 G1: WHICH contract this buys is checked by the verb, not left
    # to the skill text the 2026-09-25 buyback read and ignored.
    from tools.defi.identity_gate import buy_identity_refusal, container_of
    identity_refusal = buy_identity_refusal(
        chain=params.chain, token_out=token_out, id_out=id_out,
        max_spend_usd=params.max_spend_usd, route_verdict=sanity_verdict,
        execution_context=execution_context,
        container=container_of(tool))
    if identity_refusal:
        return tool._ar(error=identity_refusal)
    # 068 G2/B4: a run that declares its target acquires only that
    # contract. The one exit is selling a held non-canonical token into the
    # quote asset — USDC -> WETH is a buy, and is refused under a target.
    from core.wallet.buy_target import acquisition_refusal
    _t_refusal = acquisition_refusal(
        execution_context, chain=params.chain, token_out=token_out,
        token_in=(None if native_in else token_in), native_in=native_in)
    if _t_refusal:
        return tool._ar(error=_t_refusal)
    if sanity_verdict == "DISAGREES":
        # §1.2: a disagreeing route BLOCKS — it used to only narrate. A pool
        # price is a number anyone with capital can seed; when it disagrees
        # with an independent source, executing anyway is how a thin or
        # manipulated pool extracts value bounded only by amount_in.
        return tool._ar(error=(
            f"refused: route check DISAGREES — {sanity_note}. The pool is "
            f"quoting a price the independent source does not support "
            f"(drift above {_route_drift_max_pct():.0f}%); a thin or "
            f"manipulated pool extracts value this way. Nothing was "
            f"broadcast."))

    # `spender` holds the allowance; `to` is the call target. They are the
    # same contract on both providers today, but they are separate fields on
    # purpose — conflating them is exactly the class of mistake the chain
    # registry exists to prevent, so each is used for its own job.
    spender = route.spender

    # An allowance is required before the spender can pull token_in. Refuse
    # with the exact remedy rather than broadcasting a call that reverts and
    # burns gas.
    #
    # ⚠️ NOT for a native-in swap. Native value is PUSHED with the call, not
    # pulled by a spender, so there is no allowance to hold and nothing to
    # read — `read_allowance` would be asking an ERC-20 question about an
    # asset that has no contract. This is the registry's "needs no allowance
    # at all", and skipping the grant also removes a money verb, its gas and
    # its approval tap from every entry.
    # From an account (069 v4 A3) the allowance is transient: approve -> swap -> reset
    # in ONE executeBatch (W8), so no standing grant is read or needed.
    if not native_in and held is None:
        from tools.defi.providers import univ3
        allowance = univ3.read_allowance(params.chain, token_in, signer.address,
                                         spender)
        if allowance is not None and allowance < amount_in_raw:
            return tool._ar(error=(
                f"insufficient allowance: the spender may pull {allowance} but this "
                f"swap needs {amount_in_raw}. Call approve_token(token="
                f"{token_in}, spender={spender}, amount={params.amount_in}) "
                f"first, then swap, then revoke_approval."), error_kind=PRECONDITION)

    if (held is not None and not native_in
            and str(route.to).lower() != str(spender).lower()):
        return tool._ar(error=(
            f"refused: the route calls {route.to} but its spender is {spender} — an "
            f"approve-spend-reset batch from an account needs them to be the same contract. "
            f"Nothing was broadcast."))
    rail = (tool._rail_factory or EvmRail)(chain=params.chain, signer=signer)
    journal_entry, journal_skipped, journal_kind = None, "", "tend"
    try:
        tx = rail.build_call(to=route.to, data=route.calldata,
                             value=route.value_raw)
        if held is not None:
            # J1: the journal entry is signed now and rides the same account batch.
            kind_probe = "journal-kind"   # a probe intent, never sent
            journal_kind = account_mode.swap_kind(tx_guard.TxIntent(
                chain=params.chain, token=(None if native_in else token_in), to=spender,
                amount_raw=amount_in_raw, max_spend_usd=params.max_spend_usd,
                inflow_token=token_out, idempotency_key=kind_probe))
            journal_entry, journal_skipped = account_mode.prepare_journal(
                held, signer, kind=journal_kind,
                text=(f"swap {params.amount_in:g} {in_label[:8]} -> "
                      f"{id_out.symbol or token_out} on {params.chain}"),
                rpc=tool._account_rpc)
        if held is not None and native_in:
            tx, acct_state = account_mode.wrap(rail, tx, held, tool._account_rpc,
                                               journal=journal_entry)
        elif held is not None:
            tx, acct_state = account_mode.wrap_batch(
                rail, token=token_in, spender=spender, grant_raw=amount_in_raw,
                spend=tx, held=held, rpc=tool._account_rpc, journal=journal_entry)
    except Exception as exc:
        return tool._ar(error=f"could not build the transaction: {exc}")
    idem = _intent_idem("swap", params.chain, tx, execution_context)

    # The swap spends token_in and grants nothing; any allowance the
    # simulation reveals is undeclared and the guard refuses it. The
    # spender pair is MEASURED (watch_spenders) so its read delta — a
    # decrease when the spender pulls token_in — is judged as a decrease,
    # not mistaken for a grant by the event-log cross-check. Declaring
    # `inflow_token` (2026-08-26) lets the guard value an unpriceable exit
    # at the MEASURED receipt and recognize exit-shaped intents for the
    # DEFI_MONITOR_EXITS lane.
    # `token=None` DECLARES a native send (039 B1): the guard measures the
    # NATIVE delta and asserts it against amount_raw with the same symmetric
    # dust tolerance the ERC-20 branch uses. It is not "no token declared" —
    # declaring an address here instead would send the guard hunting for an
    # ERC-20 outflow that does not exist, and its `intent.token` branch
    # additionally REFUSES an unexpected native change, so the trade would be
    # refused by the guard meant to bound it. `inflow_token` is unchanged, so
    # the token actually bought is still measured and asserted either way.
    intent = tx_guard.TxIntent(
        chain=params.chain, token=(None if native_in else token_in),
        to=spender,
        amount_raw=amount_in_raw, max_spend_usd=params.max_spend_usd,
        expected_allowance_grants=(), watch_spenders=(spender,),
        idempotency_key=idem, held_balance_raw=held_balance_raw,
        inflow_token=token_out,
        # CR-H03: the route's floor is ASSERTED against the simulated
        # receipt of token_out, on every swap — not only in the monitor
        # lane. A route that takes token_in and returns nothing (or pays
        # someone else, or encodes minOut 0) is refused before signing.
        min_inflow_raw=amount_out_min)
    if held is not None:
        intent = account_mode.intent_for(intent, held, acct_state, batch=not native_in,
                                         journal=journal_entry is not None)

    out_human = route.amount_out_raw / (10 ** id_out.decimals)
    min_human = amount_out_min / (10 ** id_out.decimals)
    header = (
        f"swap {params.amount_in} "
        f"{in_label if (native_in or not id_in.symbol) else _shown_symbol(id_in.symbol, token_in)} -> "
        f"{_shown_symbol(id_out.symbol, token_out)}\n"
        + (_SYMBOL_NOTE if (id_out.symbol or (id_in and id_in.symbol)) else "") +
        (account_mode.header_line(held) if held is not None else "") +
        f"  route: {route.venue}\n"
        f"  quoted out: {out_human:.8f}  (min after {slippage}bps slippage: "
        f"{min_human:.8f})\n"
        f"  {sanity_note}\n")

    async with gate.reserve():
        result = await tool._run_guarded(
            intent=intent, tx=tx, rail=rail, gate=gate, signer=signer,
            execution_context=execution_context, header=header,
            dry_run=params.dry_run, venue_action="swap", idem=idem,
            counterparty=spender,
            # 068 G5: the audit row names what was sold and bought — the
            # idempotency key became an opaque hash on 2026-09-23 and the
            # ledger alone could no longer say which token a buy was.
            asset=f"{'native' if native_in else token_in}->{token_out}",
            amount_in_label=f"{params.amount_in:g} {in_label[:8]}",
            amount_out_label=f"≥{min_human:.8f} {id_out.symbol or token_out[:8]}",
            # 043 A35: the trade's book effect. The classifier (in core)
            # decides which side is a tracked position vs working capital;
            # cost basis is filled from the recorded USD inside _run_guarded.
            position_ctx={
                "chain": params.chain,
                "token_in": token_in,
                "token_out": token_out,
                "in_native": native_in,
                "in_symbol": in_label,
                "out_symbol": id_out.symbol or token_out,
                "in_qty": params.amount_in,
                "out_qty": out_human,
                # Authorized simulation balance deltas size both legs.
                "in_decimals": in_decimals,
                "out_decimals": id_out.decimals,
            },
            held=held, journal_kind=journal_kind, journal_entry=journal_entry,
            journal_skipped=journal_skipped)
        result.metadata = dict(result.metadata or {}, min_out_raw=amount_out_min)
        return result
