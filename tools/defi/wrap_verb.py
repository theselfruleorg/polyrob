"""``defi_trade.wrap`` / ``unwrap`` — native <-> wrapped native, 1:1, asserted by measurement.

Moved out of ``trade_tool.py`` (W9 god-file split, behaviour-preserving). The
tool keeps thin delegating methods; module-level helpers are imported from
``tools.defi.trade_tool`` at call time so a test that patches them there still
reaches this code.
"""
from __future__ import annotations

import asyncio


async def perform_wrap(tool, params, execution_context=None):
    """Native -> wrapped native, asserted by MEASUREMENT like every other verb.

    Why this verb exists (live, 2026-09-13). The treasury held 0.0512 native
    ETH on Robinhood Chain and needed WETH to trade. There was no way to turn
    one into the other: `swap` demands a contract address for `token_in` and
    has no native sentinel, and `providers/routes/lifi.py` REFUSES any quote
    carrying native value ("an unasserted native outflow riding along with
    the trade"). So the agent concluded it had to IMPORT WETH, set an
    allowance for WETH it did not hold, and spent twelve hours failing to
    bridge $3 across three routes — to acquire an asset that was already
    sitting in the same wallet in a different form.

    ⚠️ The lifi refusal's premise is now stale — `tx_guard` learned to declare
    and assert a native send in 039 B1 (`token=None`) — but relaxing it means
    asserting a native OUTFLOW and a token INFLOW in one trade, which is its
    own change. This verb does the deterministic half: one call, one pinned
    destination, 1:1, nothing to route.
    """
    from tools.defi.trade_tool import (  # call-time: tests patch these
        _DRY_RUN_HEAD, _intent_idem, _not_sent_text, _unsupported_chain)
    from core.wallet import tx_guard
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet import chains as _chains

    chain_err = _unsupported_chain(params.chain)
    if chain_err:
        return tool._ar(error=chain_err)
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)

    row = _chains.get(params.chain)
    wrapped = getattr(row, "wrapped_native", None) if row else None
    if not wrapped or not str(wrapped).startswith("0x"):
        # A chain with no pinned wrapped native (or a base58 one, i.e. Solana)
        # has nothing to wrap TO. Refusing beats inventing an address.
        return tool._ar(error=(
            f"{params.chain} pins no EVM wrapped-native address, so there is "
            f"nothing to wrap into. This verb is EVM-only."))

    signer = wallet.operational_signer()
    gate = wallet.policy
    amount_raw = int(round(params.amount * (10 ** 18)))
    if amount_raw <= 0:
        return tool._ar(error=f"amount must be positive, got {params.amount}")
    rail = (tool._rail_factory or EvmRail)(chain=params.chain, signer=signer)
    try:
        tx = rail.build_call(to=str(wrapped),
                             data=tool._WETH_DEPOSIT_SELECTOR,
                             value=amount_raw)
    except Exception as exc:
        return tool._ar(error=f"could not build the wrap transaction: {exc}")
    idem = _intent_idem("wrap", params.chain, tx, execution_context)

    # `token=None` DECLARES a native send (039 B1): the guard simulates it,
    # asserts the MEASURED native outflow against amount_raw, and prices that
    # outflow itself rather than trusting any caller's figure.
    intent = tx_guard.TxIntent(
        chain=params.chain, token=None, to=str(wrapped),
        amount_raw=amount_raw, max_spend_usd=params.max_spend_usd,
        expected_allowance_grants=(), idempotency_key=idem)

    authorize = tool._guard_fn or tx_guard.authorize
    native_symbol = getattr(row, "native_symbol", "ETH")

    async with gate.reserve():
        from tools.controller.action_registration import (
            _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)

        decision = await asyncio.to_thread(
            authorize, intent, tx, holder=signer.address, gate=gate,
            execution_context=execution_context, tool_self=tool,
            price_fn=tool._price,
            forged_fn=_is_forged_or_autonomous_turn,
            autonomous_ok_fn=_is_autonomous_goal_turn)

        header = (f"wrap {params.amount:g} {native_symbol} -> wrapped "
                  f"({wrapped}) on {params.chain}\n"
                  f"  simulated value: "
                  f"{'unknown' if decision.amount_usd is None else f'${decision.amount_usd:.4f}'}\n"
                  f"  guard: {decision.reason}\n"
                  f"  lane:  {decision.lane}\n")

        if not decision.allowed:
            return tool._ar(content=header + _not_sent_text(decision.lane))

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
                _DRY_RUN_HEAD + " Re-run with dry_run=false to wrap."))

        # Measure BEFORE. The claim this verb makes at the end is "the WETH
        # arrived", and an unread baseline cannot support it. Unlike the
        # bridge, an unreadable balance is NOT fatal here: the transaction is
        # atomic and its receipt is already proof it executed, so a failed
        # read costs the measured delta, not the guarantee.
        before = tool._held_balance_raw(params.chain, signer.address, str(wrapped))

        try:
            tx_hash = await asyncio.to_thread(rail.sign_and_send, tx)
        except Exception as exc:
            from core.wallet.broadcast.evm import broadcast_failure_text
            return tool._ar(error=broadcast_failure_text(exc, nothing="nothing was wrapped"))

        from core.wallet import tx_notify
        _used, _limit = tx_notify.caps_from_gate(gate)
        _label = f"{params.amount:g} {native_symbol}"
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="wrap", route=params.chain, chain=params.chain, amount_in=_label,
            usd=decision.amount_usd, tx_ref=tx_hash, lane=decision.lane,
            cap_used_usd=_used, cap_limit_usd=_limit), settled=False)

        _rec = dict(venue="defi", action="wrap",
                    amount_usd=decision.amount_usd or 0.0,
                    counterparty=str(wrapped), idempotency_key=idem,
                    result_ref=tx_hash, chain=params.chain)
        receipt = await tool._await_receipt_or_record(
            rail, tx_hash, gate=gate, record_kw=_rec,
            execution_context=execution_context,
            notice_kw=dict(verb="wrap", route=params.chain, chain=params.chain,
                           amount_in=_label, usd=decision.amount_usd))
        gate.record(**_rec)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="wrap", route=params.chain, chain=params.chain, amount_in=_label,
            usd=decision.amount_usd, tx_ref=tx_hash,
            state={"success": tx_notify.STATE_CONFIRMED,
                   "pending": tx_notify.STATE_IN_FLIGHT}.get(
                receipt.status, tx_notify.STATE_REVERTED),
            detail=f"wrapped on {params.chain}", ledger_recorded=True), settled=True)

    if receipt.succeeded:
        after = tool._held_balance_raw(params.chain, signer.address, str(wrapped))
        if before is None or after is None:
            measured = ("  measured: balance unreadable — the receipt confirms "
                        "the wrap executed, but the delta could not be shown\n")
        else:
            measured = (f"  measured: +{(after - before) / 10 ** 18:.18f}".rstrip("0")
                        + f" wrapped {native_symbol}\n")
        return tool._ar(content=header + measured + (
            f"  RESULT: WRAPPED AND CONFIRMED\n"
            f"  tx: {tx_hash}\n  block: {receipt.block_number}"))
    if receipt.status == "pending":
        return tool._ar(content=header + (
            f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. It may "
            f"still land — do NOT retry blindly.\n  tx: {tx_hash}"))
    return tool._ar(content=header + (
        f"  RESULT: REVERTED ON-CHAIN — the fee was spent, nothing was "
        f"wrapped.\n  tx: {tx_hash}"))


async def perform_unwrap(tool, params, execution_context=None):
    """Wrapped native -> native, asserted by MEASUREMENT on BOTH sides.

    The guard shape already existed: a TOKEN outflow with a declared minimum
    NATIVE inflow, which 042 added for a sell into a native-quoted venue.
    Nothing new is asserted here — the wrapped-native burn is bounded by the
    declared amount, and the native receipt must clear
    :func:`_unwrap_min_native_wei`.
    """
    from tools.defi.trade_tool import (  # call-time: tests patch these
        _DRY_RUN_HEAD, _intent_idem, _not_sent_text, _unsupported_chain,
        _unwrap_min_native_wei)
    from core.wallet import abi, tx_guard
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet import chains as _chains

    chain_err = _unsupported_chain(params.chain)
    if chain_err:
        return tool._ar(error=chain_err)
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)

    row = _chains.get(params.chain)
    wrapped = getattr(row, "wrapped_native", None) if row else None
    if not wrapped or not str(wrapped).startswith("0x"):
        return tool._ar(error=(
            f"{params.chain} pins no EVM wrapped-native address, so there is "
            f"nothing to unwrap. This verb is EVM-only."))

    signer = wallet.operational_signer()
    gate = wallet.policy
    amount_raw = int(round(params.amount * (10 ** 18)))
    if amount_raw <= 0:
        return tool._ar(error=f"amount must be positive, got {params.amount}")
    rail = (tool._rail_factory or EvmRail)(chain=params.chain, signer=signer)
    data = abi.encode_call("withdraw", [{"name": "wad", "type": "uint256"}],
                           [amount_raw])
    try:
        tx = rail.build_call(to=str(wrapped), data=data, value=0)
    except Exception as exc:
        return tool._ar(error=f"could not build the unwrap transaction: {exc}")
    idem = _intent_idem("unwrap", params.chain, tx, execution_context)

    held = tool._held_balance_raw(params.chain, signer.address, str(wrapped))
    intent = tx_guard.TxIntent(
        chain=params.chain, token=str(wrapped), to=str(wrapped),
        amount_raw=amount_raw, max_spend_usd=params.max_spend_usd,
        expected_allowance_grants=(), idempotency_key=idem,
        held_balance_raw=held,
        min_native_inflow_wei=_unwrap_min_native_wei(amount_raw, tx))

    authorize = tool._guard_fn or tx_guard.authorize
    native_symbol = getattr(row, "native_symbol", "ETH")

    async with gate.reserve():
        from tools.controller.action_registration import (
            _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)

        decision = await asyncio.to_thread(
            authorize, intent, tx, holder=signer.address, gate=gate,
            execution_context=execution_context, tool_self=tool,
            price_fn=tool._price,
            fallback_price_fn=tool._fallback_price,
            forged_fn=_is_forged_or_autonomous_turn,
            autonomous_ok_fn=_is_autonomous_goal_turn)

        header = (f"unwrap {params.amount:g} wrapped {native_symbol} -> "
                  f"{native_symbol} ({wrapped}) on {params.chain}\n"
                  f"  simulated value: "
                  f"{'unknown' if decision.amount_usd is None else f'${decision.amount_usd:.4f}'}\n"
                  f"  guard: {decision.reason}\n"
                  f"  lane:  {decision.lane}\n")

        if not decision.allowed:
            return tool._ar(content=header + _not_sent_text(decision.lane))

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
                _DRY_RUN_HEAD + " Re-run with dry_run=false to unwrap."))

        try:
            tx_hash = await asyncio.to_thread(rail.sign_and_send, tx)
        except Exception as exc:
            from core.wallet.broadcast.evm import broadcast_failure_text
            return tool._ar(error=broadcast_failure_text(exc, nothing="nothing was unwrapped"))

        from core.wallet import tx_notify
        _used, _limit = tx_notify.caps_from_gate(gate)
        _label = f"{params.amount:g} wrapped {native_symbol}"
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="unwrap", route=params.chain, chain=params.chain, amount_in=_label,
            usd=decision.amount_usd, tx_ref=tx_hash, lane=decision.lane,
            cap_used_usd=_used, cap_limit_usd=_limit), settled=False)

        _rec = dict(venue="defi", action="unwrap",
                    amount_usd=decision.amount_usd or 0.0,
                    counterparty=str(wrapped), idempotency_key=idem,
                    result_ref=tx_hash, chain=params.chain)
        receipt = await tool._await_receipt_or_record(
            rail, tx_hash, gate=gate, record_kw=_rec,
            execution_context=execution_context,
            notice_kw=dict(verb="unwrap", route=params.chain, chain=params.chain,
                           amount_in=_label, usd=decision.amount_usd))
        gate.record(**_rec)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="unwrap", route=params.chain, chain=params.chain, amount_in=_label,
            usd=decision.amount_usd, tx_ref=tx_hash,
            state={"success": tx_notify.STATE_CONFIRMED,
                   "pending": tx_notify.STATE_IN_FLIGHT}.get(
                receipt.status, tx_notify.STATE_REVERTED),
            detail=f"unwrapped on {params.chain}", ledger_recorded=True), settled=True)

    if receipt.succeeded:
        return tool._ar(content=header + (
            f"  RESULT: UNWRAPPED AND CONFIRMED\n"
            f"  tx: {tx_hash}\n  block: {receipt.block_number}"))
    if receipt.status == "pending":
        return tool._ar(content=header + (
            f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. It may "
            f"still land — do NOT retry blindly.\n  tx: {tx_hash}"))
    return tool._ar(content=header + (
        f"  RESULT: REVERTED ON-CHAIN — the fee was spent, nothing was "
        f"unwrapped.\n  tx: {tx_hash}"))
