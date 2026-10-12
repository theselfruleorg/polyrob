"""``defi_trade.transfer`` — send native or an ERC-20 on an EVM chain.

Moved out of ``trade_tool.py`` (W9 god-file split, behaviour-preserving). The
tool keeps a thin delegating method; module-level helpers are imported from
``tools.defi.trade_tool`` at call time so a test that patches them there still
reaches this code.
"""
from __future__ import annotations

import asyncio


async def perform_transfer(tool, params, execution_context=None):
    from tools.defi.trade_tool import (  # call-time: tests patch these
        _DRY_RUN_HEAD, _SYMBOL_NOTE, _intent_idem, _native_send_symbol, _not_sent_text,
        _shown_symbol, _unsupported_chain)
    from core.wallet import tx_guard
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.tokens import get_token_identity, normalize_address

    chain_err = _unsupported_chain(params.chain)
    if chain_err:
        return tool._ar(error=chain_err)
    wallet = tool._get_wallet()
    if wallet is None:
        return tool._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")

    native_symbol = _native_send_symbol(params.chain, params.token)
    try:
        token = None if native_symbol else normalize_address(params.token)
        to = normalize_address(params.to)
    except ValueError as exc:
        return tool._ar(error=str(exc))
    # Address poisoning (prod 2026-10-04 17:46): refuse a payee that imitates a
    # recent one (same head and tail, different body) before anything is built.
    from core.wallet import address_lookalike
    poisoned = address_lookalike.poisoning_refusal(to)
    if poisoned:
        return tool._ar(error=poisoned)

    if native_symbol:
        # A native send (2026-09-26): the gas asset has no contract, no
        # decimals() and no Transfer event. `token=None` DECLARES it to the
        # guard (039 B1), which simulates the value transfer and asserts the
        # MEASURED native outflow against amount_raw — the same guarantee
        # the ERC-20 branch gives. EVM native is 18 decimals by definition.
        ident = None
        shown = native_symbol
        amount_raw = int(round(params.amount * (10 ** 18)))
    else:
        ident = get_token_identity(params.chain, token)
        if ident.decimals is None:
            return tool._ar(error=(
                f"{token} does not report decimals — refusing to compute an "
                f"amount for a token whose denomination is unknown"))
        shown = _shown_symbol(ident.symbol, token)
        amount_raw = int(round(params.amount * (10 ** ident.decimals)))

    signer = wallet.operational_signer()
    gate = wallet.policy
    # 069 v4 A3: `account=` / `nft=` — the transfer leaves the NFT's account, not the
    # treasury; the treasury signs account.execute(...) as the NFT's owner.
    from tools.defi import account_mode
    held = None
    if account_mode.requested(params):
        held, why = account_mode.resolve(params, params.chain, signer.address,
                                         rpc=tool._account_rpc)
        if why:
            return tool._ar(error=why)
    rail = (tool._rail_factory or EvmRail)(chain=params.chain, signer=signer)
    try:
        if native_symbol:
            tx = rail.build_native_transfer(to=to, amount_wei=amount_raw)
        else:
            tx = rail.build_erc20_transfer(token=token, to=to, amount_raw=amount_raw)
        journal_entry, journal_skipped = None, ""
        if held is not None:
            # J1: the journal entry rides the same account batch (a pinned JournalLog).
            journal_entry, journal_skipped = account_mode.prepare_journal(
                held, signer, kind="tend",
                text=f"transfer {params.amount:g} {shown} -> {to} on {params.chain}",
                rpc=tool._account_rpc)
            tx, acct_state = account_mode.wrap(rail, tx, held, tool._account_rpc,
                                               journal=journal_entry)
    except Exception as exc:
        return tool._ar(error=f"could not build the transaction: {exc}")
    idem = _intent_idem("transfer", params.chain, tx, execution_context)

    intent = tx_guard.TxIntent(
        chain=params.chain, token=token, to=to, amount_raw=amount_raw,
        max_spend_usd=params.max_spend_usd, expected_allowance_grants=(),
        idempotency_key=idem)
    guard_kw = {}
    if held is not None:
        intent = account_mode.intent_for(intent, held, acct_state,
                                         journal=journal_entry is not None)
        if tool._account_rpc is not None:
            guard_kw["account_rpc"] = tool._account_rpc

    authorize = tool._guard_fn or tx_guard.authorize

    # reserve() spans authorize -> broadcast -> record so two concurrent
    # transfers cannot both clear a nearly-exhausted cap.
    async with gate.reserve():
        # Turn-origin detection lives in this tier; core cannot import it
        # (layering ratchet), and tx_guard fails closed without it.
        from tools.controller.action_registration import (
            _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)

        decision = await asyncio.to_thread(
            authorize, intent, tx, holder=signer.address, gate=gate,
            execution_context=execution_context, tool_self=tool,
            price_fn=tool._price,
            forged_fn=_is_forged_or_autonomous_turn,
            autonomous_ok_fn=_is_autonomous_goal_turn, **guard_kw)

        header = (f"transfer {params.amount} {shown} -> {to}\n"
                  + (account_mode.header_line(held) if held is not None else "")
                  + (_SYMBOL_NOTE if ident is not None and ident.symbol else "") +
                  f"  simulated value: "
                  f"{'unknown' if decision.amount_usd is None else f'${decision.amount_usd:.4f}'}\n"
                  f"  guard: {decision.reason}\n"
                  f"  lane:  {decision.lane}\n")

        if not decision.allowed:
            return tool._ar(content=header + _not_sent_text(decision.lane))

        # Same gas sizing as _run_guarded (§3a) — a transfer fits the
        # default, but the sized limit is uniformly more honest.
        if decision.sim_gas_used:
            try:
                tx = rail.size_gas(tx, decision.sim_gas_used)
            except Exception as exc:
                return tool._ar(error=(
                    f"refused at gas sizing: {exc} — nothing was broadcast"))
            header += (f"  gas:   limit {tx.get('gas')} "
                       f"(simulation used {decision.sim_gas_used})\n")

        if params.dry_run:
            return tool._ar(content=header + (
                _DRY_RUN_HEAD + " Re-run with dry_run=false to send."))

        try:
            tx_hash = await asyncio.to_thread(rail.sign_and_send, tx)
        except Exception as exc:
            from core.wallet.broadcast.evm import broadcast_error_kind, broadcast_failure_text
            return tool._ar(error=broadcast_failure_text(
                exc, nothing="funds were NOT sent"),
                error_kind=broadcast_error_kind(exc))

        from core.wallet import tx_notify
        _used, _limit = tx_notify.caps_from_gate(gate)
        _label = (f"{params.amount:g} {native_symbol}" if native_symbol
                  else f"{params.amount:g} {ident.symbol or token[:8]}")
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="transfer", route=params.chain, chain=params.chain, amount_in=_label,
            usd=decision.amount_usd, tx_ref=tx_hash, lane=decision.lane,
            cap_used_usd=_used, cap_limit_usd=_limit), settled=False)

        _rec = dict(venue="defi", action="transfer",
                    amount_usd=decision.amount_usd or 0.0,
                    counterparty=to, idempotency_key=idem, result_ref=tx_hash,
                    chain=params.chain,
                    account=(held.account if held is not None else None))
        receipt = await tool._await_receipt_or_record(
            rail, tx_hash, gate=gate, record_kw=_rec,
            execution_context=execution_context,
            notice_kw=dict(verb="transfer", route=params.chain, chain=params.chain,
                           amount_in=_label, usd=decision.amount_usd))
        gate.record(**_rec)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="transfer", route=params.chain, chain=params.chain, amount_in=_label,
            usd=decision.amount_usd, tx_ref=tx_hash,
            state={"success": tx_notify.STATE_CONFIRMED,
                   "pending": tx_notify.STATE_IN_FLIGHT}.get(
                receipt.status, tx_notify.STATE_REVERTED),
            detail=f"to {to}", ledger_recorded=True), settled=True)

    journal = ""
    if held is not None:
        journal = account_mode.journal_line(
            held, signer, kind="tend",
            text=(f"transfer {params.amount:g} {shown} -> {to} on {params.chain}: "
                  f"tx {tx_hash} ({receipt.status})"),
            refs=(tx_hash,), entry=journal_entry, landed=receipt.status != "failed",
            skipped=journal_skipped, rpc=tool._account_rpc)
    if receipt.succeeded:
        return tool._ar(content=header + (
            f"  RESULT: SENT AND CONFIRMED\n"
            f"  tx: {tx_hash}\n  block: {receipt.block_number}") + journal)
    if receipt.status == "pending":
        return tool._ar(content=header + (
            f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. The "
            f"transaction may still land — do NOT retry blindly.\n  tx: {tx_hash}") + journal)
    return tool._ar(content=header + (
        f"  RESULT: REVERTED ON-CHAIN — the transfer did NOT happen, but gas "
        f"was spent.\n  tx: {tx_hash}") + journal)
