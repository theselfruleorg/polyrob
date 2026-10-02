"""The one guarded broadcast of the core-owned ``agent_nft`` writes (069 v4 A3/A4).

Nothing here decides policy: ``tool.guard`` (= ``tx_guard.authorize``, the ONE authorizer)
does, with the same caps, pause, turn origin and owner queue as every money verb. With
``held`` the treasury — the NFT's OWNER — signs ``account.execute(inner, 0)`` and the guard
measures the account; the spend is booked with ``account=`` and a signed journal entry is
written for the account — on chain as the batch's last leg when the collection pins a
``journal_log`` (J1), else to the local journal (``core.wallet.nft_account``).
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from typing import Any, Optional


def idempotency_key(*, verb: str, chain: str, via: Optional[str], state: Optional[int],
                    to: str, value: int, data: str, hint: Any = None) -> str:
    """Derived from the call's content (a retried identical call is the same key); an account's
    ``state()`` moves with every ``execute``, so the next legitimate call gets a new key."""
    body = json.dumps({"verb": verb, "chain": chain, "via": (via or "").lower(), "state": state,
                       "to": str(to).lower(), "value": int(value), "data": str(data).lower(),
                       "hint": hint}, sort_keys=True, separators=(",", ":"), default=str)
    return f"agent_nft_{verb}:{chain}:{hashlib.sha256(body.encode('utf-8')).hexdigest()[:24]}"


def _price(chain, addr):
    # 071: the one read layer; a DISPUTED quote yields None.
    from tools.defi.price_sources import indexer_price
    return indexer_price(chain, addr)


async def guarded_call(tool, *, execution_context, verb: str, intent, inner_to: str,
                       inner_data: str, inner_value: int = 0, held=None, dry_run: bool,
                       header: str = "", rpc=None, hint: Any = None, asset: Optional[str] = None,
                       journal_kind: Optional[str] = None, journal_text: str = "",
                       counterparty: Optional[str] = None) -> Any:
    """Build → authorize → (maybe) send → confirm → record (→ journal). Returns an ActionResult."""
    from core.wallet import erc6551
    from core.wallet.broadcast.evm import EvmRail

    wallet = tool._get_wallet()
    if wallet is None:
        return tool._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
    signer = wallet.operational_signer()
    gate = wallet.policy
    chain = intent.chain
    rpc = rpc or tool.rpc_for(chain)
    try:
        rail = (getattr(tool, "_rail_factory", None) or EvmRail)(chain=chain, signer=signer)
        state = erc6551.read_state(rpc, held.account) if held is not None else None
        idem = idempotency_key(verb=verb, chain=chain, via=getattr(held, "account", None),
                               state=state, to=inner_to, value=int(inner_value), data=inner_data,
                               hint=hint)
        entry, skipped = None, ""
        if held is not None:
            leg = (inner_to, int(inner_value), inner_data, 0)
            if journal_kind:
                from tools.defi.account_mode import _journal_leg, prepare_journal
                entry, skipped = prepare_journal(held, signer, kind=journal_kind,
                                                 text=journal_text, rpc=rpc)
            data = (erc6551.encode_execute_batch([leg, _journal_leg(held, entry)])
                    if entry is not None else erc6551.encode_execute(*leg))
            tx = rail.build_call(to=held.account, value=0, data=data)
            intent = dataclasses.replace(intent, via_account=held.account, via_account_state=state,
                                         idempotency_key=idem, via_account_journal=entry is not None)
        else:
            tx = rail.build_call(to=inner_to, data=inner_data, value=int(inner_value))
            intent = dataclasses.replace(intent, idempotency_key=idem)
    except Exception as exc:  # noqa: BLE001
        return tool._ar(error=f"could not build the transaction: {exc}. Nothing was broadcast.")

    async with gate.reserve():
        from tools.controller.action_registration import (_is_autonomous_goal_turn,
                                                          _is_forged_or_autonomous_turn)
        decision = await asyncio.to_thread(
            tool.guard, intent, tx, holder=signer.address, gate=gate,
            execution_context=execution_context, tool_self=tool, price_fn=_price,
            forged_fn=_is_forged_or_autonomous_turn, autonomous_ok_fn=_is_autonomous_goal_turn,
            account_rpc=rpc)
        header += (f"  value:    {'unknown' if decision.amount_usd is None else f'${decision.amount_usd:.4f}'}\n"
                   f"  guard:    {decision.reason}\n  lane:     {decision.lane}\n")
        if not decision.allowed:
            return tool._ar(content=header + "  RESULT: NOT SENT — nothing was broadcast.")
        if decision.sim_gas_used:
            try:
                tx = rail.size_gas(tx, decision.sim_gas_used)
            except Exception as exc:  # noqa: BLE001
                return tool._ar(error=f"refused at gas sizing: {exc} — nothing was broadcast")
        if dry_run:
            return tool._ar(content=header + ("  RESULT: DRY RUN — the guard would allow this, but "
                                              "nothing was broadcast. Re-run with dry_run=false to send."))
        try:
            tx_hash = await asyncio.to_thread(rail.sign_and_send, tx)
        except Exception as exc:  # noqa: BLE001
            from core.wallet.broadcast.evm import broadcast_failure_text
            return tool._ar(error=broadcast_failure_text(exc))
        rec = dict(venue="defi", action=f"agent_nft_{verb}", amount_usd=decision.amount_usd or 0.0,
                   counterparty=counterparty or inner_to, idempotency_key=idem, result_ref=tx_hash,
                   chain=chain,
                   asset=asset, account=(held.account if held is not None else None))
        # CLI1: a cancel during the wait records the broadcast first (one seam).
        from tools.defi.receipt_wait import await_receipt_or_record
        receipt = await await_receipt_or_record(rail, tx_hash, gate=gate, record_kw=rec)
        gate.record(**rec)
    tool._last_receipt = (tx_hash, receipt, decision)
    status = {"success": "confirmed", "pending": "in flight"}.get(receipt.status, "REVERTED")
    out = header + f"  tx:       {tx_hash} ({status})"
    if held is not None and journal_kind:
        from tools.defi.account_mode import journal_line
        out += journal_line(held, signer, kind=journal_kind,
                            text=f"{journal_text}: tx {tx_hash} ({receipt.status})", refs=(tx_hash,),
                            entry=entry, landed=receipt.status != "failed", skipped=skipped,
                            rpc=rpc)
    return tool._ar(content=out)
