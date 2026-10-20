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

import asyncio
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: Default off. A money verb that ships armed is a money verb that ships wrong.
FLAG = "DEFI_CALL_ENABLED"


def call_enabled() -> bool:
    from core.env import bool_env
    return bool_env(FLAG, False)


def _typed_verb_only_destinations() -> dict:
    """Pinned contracts a caller-authored call may NEVER address (CR-H06).

    Each holds value the wallet CONTROLS but does not HOLD — an LP position,
    a Permit2 grant, a launchpad lock — so the guard, which measures holder
    balances and holder-emitted events, cannot see a call that redirects it
    (e.g. NPM ``decreaseLiquidity`` + ``collect`` to a third party reads as a
    $0 call). Only the typed verbs, which assert those effects, may reach them.
    """
    out = {}
    try:
        from core.wallet import dex_registry
        for row in dex_registry.all_rows():
            for addr, what in ((row.position_manager, "the Uniswap position manager"),
                               (row.pool_manager, "the Uniswap v4 pool manager"),
                               (row.permit2, "Permit2")):
                if addr:
                    out[addr.lower()] = what
        out[dex_registry._PERMIT2.lower()] = "Permit2"
    except Exception:
        logger.warning("typed-verb destinations: dex registry unreadable", exc_info=True)
        raise
    try:
        from tools.launchpad import pons_abi as P
        for addr in (P.FACTORY, P.ROUTER, P.DEPLOYER, P.MEME_HOOK, P.LOCKER):
            out[addr.lower()] = "a pinned Pons launchpad contract"
    except Exception:
        logger.warning("typed-verb destinations: launchpad pins unreadable", exc_info=True)
        raise
    return out


def typed_verb_only_refusal(to: str) -> Optional[str]:
    """Why a generic call / dapp transaction to ``to`` is refused, or None.

    Fails CLOSED: a pin table that cannot be read refuses the call.
    """
    try:
        pinned = _typed_verb_only_destinations()
    except Exception as exc:
        return (f"refused: the pinned-contract table could not be read ({exc}); "
                f"failing closed")
    what = pinned.get(str(to or "").lower())
    if what is None:
        return None
    return (f"refused: {to} is {what}. It holds value the wallet controls but "
            f"does not hold (positions, grants, locks), which this guard cannot "
            f"measure — use the typed verb (lp_* / launchpad_* / approve_token), "
            f"never a generic call")


def intent_idempotency_key(prefix: str, *, chain: str, to: str, data: str,
                           value_wei: int, tx: Optional[dict] = None,
                           execution_context=None) -> str:
    """A replay key derived from the STABLE intent (CR-L02) — never a uuid.

    ``(chain, to, sha256(calldata), value, nonce, session)``. The nonce of the
    built transaction makes a deliberate repeat (a later nonce) a new key
    while a double submission of the same built transaction collides, so the
    PolicyGate replay guard is live instead of inert.
    """
    digest = hashlib.sha256((data or "0x").lower().encode("ascii", "replace")).hexdigest()
    nonce = (tx or {}).get("nonce")
    session = getattr(execution_context, "session_id", None) or "-"
    return (f"{prefix}:{chain}:{str(to or '').lower()}:{digest[:32]}:"
            f"{int(value_wei or 0)}:n{nonce if nonce is not None else '?'}:{session}")


#: ``transfer(address,uint256)`` / ``transferFrom(address,address,uint256)``.
_SEL_TRANSFER = "a9059cbb"
_SEL_TRANSFER_FROM = "23b872dd"


def _erc20_payee(raw: bytes) -> Optional[str]:
    """The recipient an ERC-20 ``transfer``/``transferFrom`` calldata names, or None.

    The low 20 bytes of the address word: a pre-0.8 token masks dirty high bits
    and pays exactly that address (a 0.8 token reverts), so the guard binds the
    address the token would actually pay."""
    sel, body = raw[:4].hex(), raw[4:]
    word = None
    if sel == _SEL_TRANSFER and len(body) >= 64:
        word = body[:32]
    elif sel == _SEL_TRANSFER_FROM and len(body) >= 96:
        word = body[32:64]
    if word is None:
        return None
    return "0x" + word[12:].hex()


def _is_erc20_transfer(raw: bytes) -> bool:
    return raw[:4].hex() in (_SEL_TRANSFER, _SEL_TRANSFER_FROM)


def payout_card_context(params: dict) -> list:
    """The owner's approval card for a payout-shaped call names who is paid
    (085 W2): the decoded ERC-20 payee, or the call target for a native value."""
    from core.config_policy.spend_lane import payout_shaped
    if not payout_shaped("defi_trade_call", params):
        return []
    try:
        data = str(params.get("calldata") or "").strip()
        raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    except ValueError:
        raw = b""
    payee = _erc20_payee(raw) if _is_erc20_transfer(raw) else None
    paid = (f"{payee} (decoded from the token transfer calldata)" if payee
            else f"{params.get('to')} (the call target)")
    return [f"⚠️ PAYOUT: this call moves value and declares nothing coming back. "
            f"It pays {paid}. Check every character against what you asked for."]


def _spend_price(chain: str, address: str):
    from tools.defi.price_sources import spend_price
    return spend_price(chain, address)


def _register_card_context() -> None:
    from tools.controller.grant_card import register_card_context
    register_card_context("defi_trade_call", payout_card_context)
    # The spend-grade price the core payout predicate (the controller hook's
    # spend_exemption) values a declared inflow with (085 W2, money M-1).
    from core.config_policy.spend_lane import register_spend_price
    register_spend_price(_spend_price)


_register_card_context()


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

    from core.wallet.tokens import raw_amount
    try:
        value_wei = raw_amount(params.value or 0, 18)
    except ValueError as exc:
        return tool._ar(error=f"refused: {exc}")

    try:
        to_addr = normalize_address(params.to)
    except Exception as exc:
        return tool._ar(error=f"refused: `to` is not a valid address ({exc})")
    pinned_err = typed_verb_only_refusal(to_addr)
    if pinned_err:
        return tool._ar(error=pinned_err + " Nothing was broadcast.")

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

    # 085 W2: classify the call shape with the ONE predicate the controller hook
    # reads. A fair inflow (a canonical or own receive_token worth >= half the
    # outflow at the spend-grade price) keeps the capped defi lane. Any other call
    # that moves value can pay an arbitrary address: the guard queues it for the
    # owner in every lane, and the spend lane keeps the tap on an owner turn too.
    from core.config_policy.spend_lane import payout_shaped
    payout = await asyncio.to_thread(payout_shaped, "defi_trade_call", {
        "chain": params.chain, "value": params.value, "spend_token": spend_token,
        "spend_max_raw": params.spend_max_raw, "receive_token": receive_token,
        "receive_min_raw": params.receive_min_raw}, price_fn=tool._price)
    intent_to = to_addr
    recipient = _erc20_payee(raw)
    on_spend_token = bool(spend_token) and spend_token.lower() == to_addr.lower()
    if on_spend_token and _is_erc20_transfer(raw) and recipient is None:
        return tool._ar(error=(
            "refused: the token transfer calldata is too short to name its "
            "recipient — an old token would pad it and pay an address nobody "
            "wrote. Nothing was broadcast."))
    if recipient and on_spend_token:
        # A transfer on the declared token: bind the guard's 6b destination
        # check to the decoded payee, not to the token contract.
        intent_to = recipient
    from core.wallet import address_lookalike
    for payee in {a for a in (recipient, to_addr if payout else None) if a}:
        poisoned = address_lookalike.poisoning_refusal(payee)
        if poisoned:
            return tool._ar(error=poisoned)

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
        # 085 W2 (money M-1): the CR-H02 rule approve_token applies — a grant to
        # a spender this chain does not pin needs a genuine owner turn.
        from core.wallet import tx_guard as _txg
        if spender.lower() not in _txg.pinned_route_spenders(params.chain):
            refusal = tool._non_route_grant_refusal(execution_context, spender)
            if refusal:
                return tool._ar(error=refusal)
        grants = ((spend_token, spender, int(params.allow_max_raw)),)
    elif spend_token:
        # Measure the callee as a spender without declaring a grant: many
        # protocols pull through the contract being called, and an OZ-4.x token
        # re-emits Approval(remaining) on transferFrom, which reads as a hidden
        # grant unless it is measured.
        watch = (to_addr,)

    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)
    signer = wallet.operational_signer()
    gate = wallet.policy

    rail = (tool._rail_factory or EvmRail)(chain=params.chain, signer=signer)
    try:
        tx = rail.build_call(to=to_addr, data=data, value=value_wei)
    except Exception as exc:
        return tool._ar(error=f"could not build the call: {exc}")
    idem = intent_idempotency_key(
        "defi_call", chain=params.chain, to=to_addr, data=data,
        value_wei=value_wei, tx=tx, execution_context=execution_context)

    intent = tx_guard.TxIntent(
        chain=params.chain,
        token=spend_token,
        to=intent_to,
        amount_raw=(int(params.spend_max_raw) if spend_token else value_wei),
        max_spend_usd=params.max_spend_usd,
        expected_allowance_grants=grants,
        watch_spenders=watch,
        idempotency_key=idem,
        inflow_token=receive_token,
        min_inflow_raw=(int(params.receive_min_raw) or None),
        payout_shape=payout)

    authorize = tool._guard_fn or tx_guard.authorize
    async with gate.reserve():
        from tools.controller.action_registration import (
            _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)

        # CR-M10: the guard's RPC/simulation and the receipt poll are blocking;
        # a thread keeps the event loop (owner /stop, Telegram) alive meanwhile.
        decision = await asyncio.to_thread(
                             authorize, intent, tx, holder=signer.address, gate=gate,
                             execution_context=execution_context, tool_self=tool,
                             price_fn=tool._price,
                             fallback_price_fn=tool._fallback_price,
                             forged_fn=_is_forged_or_autonomous_turn,
                             autonomous_ok_fn=_is_autonomous_goal_turn)

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
                tx = await asyncio.to_thread(rail.size_gas, tx, decision.sim_gas_used)
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
            tx_hash = await asyncio.to_thread(rail.sign_and_send, tx)
        except Exception as exc:
            from core.wallet.broadcast.evm import broadcast_failure_text
            return tool._ar(error=broadcast_failure_text(exc))

        _used, _limit = tx_notify.caps_from_gate(gate)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb="call", route=f"{params.chain}:{to_addr}", chain=params.chain,
            amount_in=data[:10],
            usd=decision.amount_usd, tx_ref=tx_hash, lane=decision.lane,
            cap_used_usd=_used, cap_limit_usd=_limit), settled=False)

        _rec = dict(venue="defi", action="call",
                    amount_usd=decision.amount_usd or 0.0,
                    counterparty=to_addr, idempotency_key=idem,
                    result_ref=tx_hash, chain=params.chain)
        # CLI1: a cancel during the wait records the broadcast first (one seam).
        from tools.defi.receipt_wait import await_receipt_or_record
        receipt = await await_receipt_or_record(
            rail, tx_hash, gate=gate, record_kw=_rec, tool=tool,
            execution_context=execution_context,
            notice_kw=dict(verb="call", route=f"{params.chain}:{to_addr}",
                           chain=params.chain, amount_in=data[:10],
                           usd=decision.amount_usd))
        gate.record(**_rec)
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
