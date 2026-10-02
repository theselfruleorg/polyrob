"""The shared guarded broadcast for every launchpad write (042).

One body, three intent shapes, so a launch, a buy and a sell cannot drift into
three different ideas of what is asserted:

* **launch / buy on a native-quoted curve** — a native outflow (``token=None``),
  asserted against the declared amount;
* **buy on an ERC-20-quoted curve** — a token outflow plus a declared minimum
  token inflow;
* **sell into a native-quoted curve** — a token outflow plus a declared minimum
  NATIVE inflow (``min_native_inflow_wei``), which is the assertion 042 added
  precisely so this shape stops being unrepresentable;
* **claim** — NO outflow at all, plus a declared minimum native inflow
  (``is_claim``). Its cost is the fee; the receipt is what is asserted.

Nothing here decides policy. ``tx_guard.authorize`` does, exactly as it does for
every other money verb, with the same caps, the same daily cap, the same 031
pause and the same owner queue above the ceiling.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def guarded_send(tool, *, execution_context, verb: str, chain: str,
                       to: str, calldata: str, value_wei: int,
                       max_spend_usd: float, dry_run: bool, header: str,
                       spend_token: Optional[str] = None,
                       spend_max_raw: int = 0,
                       receive_token: Optional[str] = None,
                       min_inflow_raw: Optional[int] = None,
                       min_native_inflow_wei: Optional[int] = None,
                       is_claim: bool = False,
                       watch_spenders: tuple = (),
                       on_receipt=None, held=None) -> Any:
    """Authorize, size, send, confirm. Returns the tool's ActionResult.

    ``on_receipt(rail, tx_hash)``, when given, is called once the receipt has
    confirmed. It may return either the legacy ``str`` (extra report text
    appended to the response) or a ``(str, Optional[dict])`` pair — the dict's
    ``"token"`` key, when present, names the address the durable spend record
    should carry (043 A36: a launch creates a token `to` never names; `to` is
    only the factory it was launched THROUGH).
    """
    from core.wallet import tx_guard, tx_notify
    from core.wallet.broadcast.evm import EvmRail

    wallet = tool._get_wallet()
    if wallet is None:
        return tool._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
    signer = wallet.operational_signer()
    gate = wallet.policy
    idem = f"launchpad_{verb}:{chain}:{to}:{uuid.uuid4().hex[:8]}"

    rail = (tool._rail_factory or EvmRail)(chain=chain, signer=signer)
    acct_state = None
    journal_entry, journal_skipped = None, ""
    try:
        tx = rail.build_call(to=to, data=calldata, value=value_wei)
        if held is not None:
            # 069 v4 A3: the treasury signs account.execute(...) as the NFT's owner; J1: its
            # journal entry rides the same batch when the collection pins a JournalLog.
            from tools.defi import account_mode
            journal_entry, journal_skipped = account_mode.prepare_journal(
                held, signer, kind="tend", text=f"launchpad {verb} on {chain} via {to}",
                rpc=getattr(tool, "_rpc", None))
            tx, acct_state = account_mode.wrap(rail, tx, held, getattr(tool, "_rpc", None),
                                               journal=journal_entry)
    except Exception as exc:
        return tool._ar(error=f"could not build the transaction: {exc}")

    intent = tx_guard.TxIntent(
        chain=chain,
        token=spend_token,
        to=to,
        amount_raw=(int(spend_max_raw) if spend_token else int(value_wei)),
        max_spend_usd=max_spend_usd,
        watch_spenders=watch_spenders,
        idempotency_key=idem,
        inflow_token=receive_token,
        min_inflow_raw=min_inflow_raw,
        min_native_inflow_wei=min_native_inflow_wei,
        is_claim=is_claim)
    guard_kw = {}
    if held is not None:
        from tools.defi import account_mode
        intent = account_mode.intent_for(intent, held, acct_state,
                                         journal=journal_entry is not None)
        if getattr(tool, "_rpc", None) is not None:
            guard_kw["account_rpc"] = tool._rpc
        header += account_mode.header_line(held)

    authorize = tool._guard_fn or tx_guard.authorize
    async with gate.reserve():
        from tools.controller.action_registration import (
            _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)

        # CR-M10: simulation, signing RPC and receipt polling run off the
        # event loop so the held reservation never freezes other sessions.
        decision = await asyncio.to_thread(
            authorize, intent, tx, holder=signer.address, gate=gate,
            execution_context=execution_context, tool_self=tool,
            price_fn=tool._price,
            fallback_price_fn=tool._fallback_price,
            forged_fn=_is_forged_or_autonomous_turn,
            autonomous_ok_fn=_is_autonomous_goal_turn, **guard_kw)

        usd = ('unknown' if decision.amount_usd is None
               else f'${decision.amount_usd:.4f}')
        if is_claim:
            # A claim has no outflow, so the guard's amount is the gas fee. Called
            # "value" it read as the price of what was claimed (live 2026-09-29:
            # 0.3087 ETH reported to the owner as worth two cents).
            header += (f"  cost:     {usd} — the claim's gas fee, NOT the value "
                       f"of what is claimed\n")
        else:
            header += f"  value:    {usd}\n"
        header += (
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

        if dry_run:
            return tool._ar(content=header + (
                "  RESULT: DRY RUN — the guard would allow this, but nothing "
                "was broadcast. Re-run with dry_run=false to send."))

        try:
            tx_hash = await asyncio.to_thread(rail.sign_and_send, tx)
        except Exception as exc:
            from core.wallet.broadcast.evm import broadcast_failure_text
            return tool._ar(error=broadcast_failure_text(exc))

        used, limit = tx_notify.caps_from_gate(gate)
        tool._notify_tx(execution_context, tx_notify.TxNotice(
            verb=f"launchpad {verb}", route=f"{chain}:{to}", chain=chain,
            amount_in=calldata[:10], usd=decision.amount_usd, tx_ref=tx_hash,
            lane=decision.lane, cap_used_usd=used, cap_limit_usd=limit),
            settled=False)

        # CLI1: a cancel in the receipt wait (or the read-back below) records the
        # broadcast first, against the factory/curve — the ONE seam.
        from tools.defi.receipt_wait import await_receipt_or_record
        interrupted = dict(
            gate=gate, tool=tool, execution_context=execution_context,
            record_kw=dict(venue="defi", action=f"launchpad_{verb}",
                           amount_usd=decision.amount_usd or 0.0, counterparty=to,
                           idempotency_key=idem, result_ref=tx_hash, chain=chain,
                           account=(held.account if held is not None else None)),
            notice_kw=dict(verb=f"launchpad {verb}", route=f"{chain}:{to}", chain=chain,
                           amount_in=calldata[:10], usd=decision.amount_usd))
        receipt = await await_receipt_or_record(rail, tx_hash, **interrupted)

    # The SETTLED notice is emitted below, after the record (043 T2). It used to
    # fire here, inside the reservation, carrying `ledger_recorded=True` — but
    # A36 deferred the record past the `on_receipt` read underneath it, so that
    # flag was a claim about something that had not happened yet and could still
    # raise. `render_settled` prints a loud "⚠ ledger: NOT recorded" on `False`,
    # so a premature `True` is the exact confident-and-wrong shape a money
    # notice may never have.
    def _settled_notice(recorded: bool) -> None:
        try:
            tool._notify_tx(execution_context, tx_notify.TxNotice(
                verb=f"launchpad {verb}", route=f"{chain}:{to}", chain=chain,
                amount_in=calldata[:10], usd=decision.amount_usd, tx_ref=tx_hash,
                state={"success": tx_notify.STATE_CONFIRMED,
                       "pending": tx_notify.STATE_IN_FLIGHT}.get(
                    receipt.status, tx_notify.STATE_REVERTED),
                detail=f"launchpad {verb} on {chain}",
                ledger_recorded=recorded), settled=True)
        except Exception:
            # Runs from a `finally` on the money path: a notice that cannot be
            # built must never mask the ledger failure it is reporting.
            logger.debug("launchpad: settled notice failed", exc_info=True)

    # 043 A36: `to` is the FACTORY (launch) or the CURVE (buy/sell) — a launch
    # actually creates a new token at an address `to` never names. `on_receipt`
    # may return either the legacy `str` (extra report text) or a
    # `(str, Optional[dict])` pair, where the dict's `"token"` key — when
    # present — is the address the record should name. Falling back to `to`
    # (the factory) is the honest answer when no event was found in the
    # receipt; the callback's own text already carries a "check the explorer"
    # warning for that case.
    counterparty = to
    extra = ""
    if receipt.succeeded and on_receipt is not None:
        from tools.defi.receipt_wait import record_on_interrupt
        try:
            with record_on_interrupt(tx_hash, **interrupted):   # CLI1
                result = await asyncio.to_thread(on_receipt, rail, tx_hash)
        except Exception as exc:
            logger.debug("launchpad: receipt read failed (%s)", exc)
            result = ("  ⚠️ the transaction confirmed but its receipt could "
                      "not be read back — check the explorer for what it "
                      "created.\n")
        if isinstance(result, tuple):
            extra, found = result
            extra = extra or ""
            if found and found.get("token"):
                counterparty = found["token"]
                if verb == "launch":
                    # W0: a confirmed launch is our own token — recorded as
                    # provenance so the owner never pins their own launch.
                    from core.wallet.token_provenance import record_own_token
                    record_own_token(chain, counterparty, kind="launchpad_launch",
                                     evidence=f"tx {tx_hash}")
        else:
            extra = result or ""

    # Fix round 1 (043 A36 review): re-acquire the reserve lock for the record
    # alone rather than widening the FIRST reservation across the on_receipt
    # read above — the token address is only known after that read, so the
    # record is deliberately deferred out of that window. The residual race:
    # a concurrent verb's check() can pass a nearly-exhausted cap during the
    # read (bounded by one receipt read), over-running the cap by at most one
    # transaction <= the per-tx ceiling. A spend is never left unrecorded.
    #
    # The settled notice rides the `finally` so it is emitted exactly once on
    # every path, carrying what the record actually did. A raising record still
    # propagates — an unrecorded spend stays a loud failure — but the owner
    # learns of it in the same notice that reports the transaction, rather than
    # reading a clean line over an invisible spend.
    recorded = False
    try:
        async with gate.reserve():
            gate.record(venue="defi", action=f"launchpad_{verb}",
                        amount_usd=decision.amount_usd or 0.0, counterparty=counterparty,
                        idempotency_key=idem, result_ref=tx_hash, chain=chain,
                        account=(held.account if held is not None else None))
        recorded = True
    finally:
        _settled_notice(recorded)

    if held is not None:
        from tools.defi import account_mode
        extra += account_mode.journal_line(
            held, signer, kind="tend",
            text=f"launchpad {verb} on {chain} via {to}: tx {tx_hash} ({receipt.status})",
            refs=(tx_hash,), entry=journal_entry, landed=receipt.status != "failed",
            skipped=journal_skipped, rpc=getattr(tool, "_rpc", None)).lstrip("\n") + "\n"
    if receipt.succeeded:
        return tool._ar(content=header + extra + (
            f"  RESULT: CONFIRMED\n  tx: {tx_hash}\n"
            f"  block: {receipt.block_number}"))
    if receipt.status == "pending":
        return tool._ar(content=header + (
            f"  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout. It may "
            f"still land — do NOT retry blindly, a second launch creates a "
            f"SECOND token.\n  tx: {tx_hash}"))
    return tool._ar(content=header + (
        f"  RESULT: REVERTED ON-CHAIN — the fee was spent, nothing else "
        f"happened.\n  tx: {tx_hash}"))


def receipt_logs(rail, tx_hash: str) -> Optional[Dict[str, Any]]:
    try:
        return rail._rpc("eth_getTransactionReceipt", [tx_hash]) or None
    except Exception as exc:
        logger.debug("launchpad: receipt fetch failed (%s)", exc)
        return None
