"""The generic contract-write verb — ``defi_trade_call`` (042, designed in 038 §3.3).

Until this existed the agent could READ any protocol perfectly and still not
supply a dollar to one: every money verb took typed parameters and built its own
calldata in-process, so a protocol nobody had integrated ahead of time was
reachable but not usable. The Aave MCP is the canonical case — it prepares
UNSIGNED transactions and never holds a key, which is worth nothing without a
verb that can execute one.

038 deferred this on purpose and named the one thing missing:

    *"A call that took USDC and returned no aUSDC clears every current check …
    For a curated swap the route provider's min-out covers this; for an
    arbitrary call nothing does."*

That is ``TxIntent.min_inflow_raw``, built in the same wave. With it, the caller
declares BOTH directions — at most this much leaves, at least this much comes
back — and the SIMULATION, not the caller, decides whether the declaration held.

⚠️ The honest bound, restated from ``tx_guard``'s own docstring: delta assertion
proves the transaction does what the intent SAYS; it cannot prove the intent is
legitimate. If a prompt injection authors both the calldata and the declaration,
they will agree. What bounds THAT is the turn-origin refusal, the per-transaction
ceiling, the rolling daily cap and the owner queue — all of which this verb
inherits unchanged.
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

#: Default off. A money verb that ships armed is a money verb that ships wrong.
FLAG = "DEFI_CALL_ENABLED"


def call_enabled() -> bool:
    from core.env import bool_env
    return bool_env(FLAG, False)


async def perform_call(tool, params, execution_context=None):
    from core.wallet import tx_guard, tx_notify
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.tokens import normalize_address
    from tools.defi.deploy_verb import _refuse_non_owner_turn, _refuse_paused
    from tools.defi.trade_tool import _unsupported_chain

    if not call_enabled():
        return tool._ar(error=(
            f"the generic contract call is off — set {FLAG}=true to arm it. "
            f"Nothing was broadcast."))

    chain_err = _unsupported_chain(params.chain)
    if chain_err:
        return tool._ar(error=chain_err)
    turn_err = _refuse_non_owner_turn(execution_context, "call a contract")
    if turn_err:
        return tool._ar(error=turn_err)
    if not params.dry_run:
        paused = _refuse_paused()
        if paused:
            return tool._ar(error=paused + " RESULT: NOT SENT.")

    data = (params.calldata or "").strip()
    if not data.startswith("0x"):
        data = "0x" + data
    try:
        raw = bytes.fromhex(data[2:])
    except ValueError:
        return tool._ar(error="refused: calldata is not valid hex")
    if len(raw) < 4:
        return tool._ar(error=(
            "refused: calldata is shorter than a 4-byte selector — a call with "
            "no function to call is a native transfer, and that is `transfer`"))

    value_wei = int(round(float(params.value or 0.0) * 10 ** 18))

    try:
        to_addr = normalize_address(params.to)
    except Exception as exc:
        return tool._ar(error=f"refused: `to` is not a valid address ({exc})")

    # The guard's ERC-20 branch refuses ANY material native movement alongside a
    # declared token outflow ("the transaction does something that was not
    # declared"). That rule is right and is not being relaxed here, so the verb
    # states the constraint up front instead of letting the guard refuse it later
    # with a message about a check the caller never heard of.
    if value_wei > 0 and params.spend_token:
        return tool._ar(error=(
            "refused: declare EITHER a native value OR a token outflow, not "
            "both. The guard asserts one outflow against one declaration; a "
            "call that moves native AND a token cannot be held to either."))

    if params.receive_min_raw and not params.receive_token:
        return tool._ar(error=(
            "refused: receive_min_raw was declared with no receive_token to "
            "measure it on"))

    spend_token = None
    if params.spend_token:
        try:
            spend_token = normalize_address(params.spend_token)
        except Exception as exc:
            return tool._ar(error=f"refused: spend_token is not a valid address ({exc})")
    receive_token = None
    if params.receive_token:
        try:
            receive_token = normalize_address(params.receive_token)
        except Exception as exc:
            return tool._ar(error=f"refused: receive_token is not a valid address ({exc})")

    grants = ()
    watch = ()
    if params.allow_spender:
        try:
            spender = normalize_address(params.allow_spender)
        except Exception as exc:
            return tool._ar(error=f"refused: allow_spender is not a valid address ({exc})")
        if not spend_token:
            return tool._ar(error=(
                "refused: an allowance is a claim on a TOKEN — declare the "
                "spend_token the grant is on"))
        # S4 (2026-09-14): the floor `approve_token` has always applied belongs
        # to every verb that can create a standing claim. Without it the one
        # grant shape the typed verb refuses outright was reachable here.
        from tools.defi.trade_tool import _UNLIMITED_APPROVAL_FLOOR
        if int(params.allow_max_raw) >= _UNLIMITED_APPROVAL_FLOOR:
            return tool._ar(error=(
                "refused: that is an effectively UNLIMITED approval. An unlimited "
                "grant outlives the call and remains a standing claim on the "
                "wallet. Declare the exact amount this call needs."))
        grants = ((spend_token, spender, int(params.allow_max_raw)),)
    elif spend_token:
        # Measure the callee as a spender without declaring a grant: many
        # protocols pull through the contract being called, and an OZ-4.x token
        # re-emits Approval(remaining) on transferFrom, which reads as a hidden
        # grant unless it is measured.
        watch = (to_addr,)

    wallet = tool._get_wallet()
    if wallet is None:
        return tool._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
    signer = wallet.operational_signer()
    gate = wallet.policy
    idem = f"defi_call:{params.chain}:{to_addr}:{data[:10]}:{uuid.uuid4().hex[:8]}"

    rail = (tool._rail_factory or EvmRail)(chain=params.chain, signer=signer)
    try:
        tx = rail.build_call(to=to_addr, data=data, value=value_wei)
    except Exception as exc:
        return tool._ar(error=f"could not build the call: {exc}")

    intent = tx_guard.TxIntent(
        chain=params.chain,
        token=spend_token,
        to=to_addr,
        amount_raw=(int(params.spend_max_raw) if spend_token else value_wei),
        max_spend_usd=params.max_spend_usd,
        expected_allowance_grants=grants,
        watch_spenders=watch,
        idempotency_key=idem,
        inflow_token=receive_token,
        min_inflow_raw=(int(params.receive_min_raw) or None))

    authorize = tool._guard_fn or tx_guard.authorize
    async with gate.reserve():
        from tools.controller.action_registration import _is_forged_or_autonomous_turn

        decision = authorize(intent, tx, holder=signer.address, gate=gate,
                             execution_context=execution_context, tool_self=tool,
                             price_fn=tool._price,
                             fallback_price_fn=tool._fallback_price,
                             forged_fn=_is_forged_or_autonomous_turn)

        header = (
            f"call {data[:10]} on {to_addr} ({params.chain})\n"
            f"  spends:   "
            f"{f'{params.spend_max_raw} raw of {spend_token}' if spend_token else f'{params.value:g} native'} (max)\n"
            f"  receives: "
            f"{f'at least {params.receive_min_raw} raw of {receive_token}' if params.receive_min_raw else 'nothing declared'}\n"
            f"  value:    "
            f"{'unknown' if decision.amount_usd is None else f'${decision.amount_usd:.4f}'}\n"
            f"  guard:    {decision.reason}\n"
            f"  lane:     {decision.lane}\n")

        if not decision.allowed:
            return tool._ar(content=header + "  RESULT: NOT SENT — nothing was broadcast.")

        if decision.sim_gas_used:
            try:
                tx = rail.size_gas(tx, decision.sim_gas_used)
            except Exception as exc:
                return tool._ar(error=(
                    f"refused at gas sizing: {exc} — nothing was broadcast"))
            header += (f"  gas:      limit {tx.get('gas')} "
                       f"(simulation used {decision.sim_gas_used})\n")

        if params.dry_run:
            return tool._ar(content=header + (
                "  RESULT: DRY RUN — the guard would allow this, but nothing was "
                "broadcast. Re-run with dry_run=false to send."))

        try:
            tx_hash = rail.sign_and_send(tx)
        except Exception as exc:
            return tool._ar(error=f"broadcast failed: {exc} — nothing was sent")

        _used, _limit = tx_notify.caps_from_gate(gate)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="call", route=f"{params.chain}:{to_addr}", chain=params.chain,
            amount_in=data[:10],
            usd=decision.amount_usd, tx_ref=tx_hash, lane=decision.lane,
            cap_used_usd=_used, cap_limit_usd=_limit), settled=False)

        receipt = rail.await_receipt(tx_hash)
        gate.record(venue="defi", action="call",
                    amount_usd=decision.amount_usd or 0.0,
                    counterparty=to_addr, idempotency_key=idem,
                    result_ref=tx_hash, chain=params.chain)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="call", route=f"{params.chain}:{to_addr}", chain=params.chain,
            amount_in=data[:10],
            usd=decision.amount_usd, tx_ref=tx_hash,
            state={"success": tx_notify.STATE_CONFIRMED,
                   "pending": tx_notify.STATE_IN_FLIGHT}.get(
                receipt.status, tx_notify.STATE_REVERTED),
            detail=f"contract call on {params.chain}", ledger_recorded=True),
            settled=True)

    if receipt.succeeded:
        return tool._ar(content=header + (
            f"  RESULT: SENT AND CONFIRMED\n"
            f"  tx: {tx_hash}\n  block: {receipt.block_number}"))
    if receipt.status == "pending":
        return tool._ar(content=header + (
            f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. It may "
            f"still land — do NOT retry blindly.\n  tx: {tx_hash}"))
    return tool._ar(content=header + (
        f"  RESULT: REVERTED ON-CHAIN — the fee was spent, nothing else "
        f"happened.\n  tx: {tx_hash}"))
