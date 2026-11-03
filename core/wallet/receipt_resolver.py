"""Resolve the receipt of a money tx booked before its receipt was known (098 TS-23).

A DeFi verb books its broadcast as ``receipt_state = "pending_receipt"`` when
the receipt wait was interrupted or timed out (``tools/defi/receipt_wait.py``,
``call_verb``, ``trade_tool``). This pass (run by the bridge watcher tick) reads
``eth_getTransactionReceipt`` on the chain's pinned RPC and appends ONE
resolution through :meth:`core.money.ledger.SpendLedger.note_receipt`:

* status 1 → ``confirmed``;
* status 0 → ``reverted`` — the entry leaves the rolling caps (voided);
* no receipt 24 h after the booking → ``unknown`` (the owner is told once).

Reads only; never signs, sends or releases a submission. An RPC error, an
unknown chain or a chain-id mismatch leaves the entry pending (fail-open).
Not resolved yet (reported, see the 098 close-out): the position delta of a
confirmed interrupted swap, and EVM rows only (no Solana).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _default_call(chain: str, method: str, params: list):
    from core.wallet.onchain import _rpc, rpc_url_for_chain
    url = rpc_url_for_chain(chain)
    if not url:
        raise ValueError("no RPC endpoint for the chain")
    return _rpc(url, method, params, timeout=5)


def _quantity(value) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value):
        raise ValueError("invalid RPC quantity")
    return int(value, 16)


def _read_receipt(entry: dict, call: Callable) -> Optional[Dict[str, Any]]:
    """``{"status": 0|1, "block": n}``, None while there is no receipt. Raises
    on any doubt (wrong chain, identity mismatch, bad data)."""
    from core.wallet import chains
    chain, ref = str(entry.get("chain") or ""), str(entry.get("result_ref") or "")
    row = chains.get(chain)
    if row is None or getattr(row, "family", "evm") != "evm":
        raise ValueError("not an EVM chain")
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", ref):
        raise ValueError("not an EVM tx hash")
    if _quantity(call(chain, "eth_chainId", [])) != row.chain_id:
        raise ValueError("RPC chain differs from the configured chain")
    receipt = call(chain, "eth_getTransactionReceipt", [ref])
    if receipt is None:
        return None
    if str(receipt.get("transactionHash") or "").lower() != ref.lower():
        raise ValueError("receipt identity mismatch")
    status = _quantity(receipt.get("status"))
    if status not in (0, 1):
        raise ValueError("invalid receipt status")
    return {"status": status, "block": _quantity(receipt.get("blockNumber"))}


async def resolve_pending_receipts(gate: Any = None, *, call: Optional[Callable] = None,
                                   now: Optional[float] = None) -> List[Dict[str, Any]]:
    """One pass. Returns ``[{ref, chain, state, amount_usd}]`` for each entry it
    resolved. Never raises."""
    from core.money.receipt_state import UNKNOWN_AFTER_SEC, open_receipts
    try:
        if gate is None:
            from core.wallet.factory import get_policy_gate
            gate = get_policy_gate()
        pending = open_receipts(list(gate.audit_log))
    except Exception:
        logger.debug("receipt resolver: no spend ledger", exc_info=True)
        return []
    call = call or _default_call
    t = time.time() if now is None else float(now)
    out = []
    for entry in pending:
        try:
            got = await asyncio.to_thread(_read_receipt, entry, call)
        except Exception as exc:  # noqa: BLE001 — never relay RPC text (URLs hold keys)
            logger.debug("receipt resolver: %s unreadable (%s)", entry.get("result_ref"),
                         type(exc).__name__)
            continue
        if got is not None:
            state, block = ("confirmed" if got["status"] == 1 else "reverted"), got["block"]
        elif t - float(entry.get("ts") or t) > UNKNOWN_AFTER_SEC:
            state, block = "unknown", None
        else:
            continue
        try:
            async with gate.reserve():
                moved = gate.note_receipt(entry, state, block=block)
        except Exception:
            logger.warning("receipt resolver: could not record %s", entry.get("result_ref"),
                           exc_info=True)
            continue
        if not moved:
            continue
        item = {"ref": entry.get("result_ref"), "chain": entry.get("chain"), "state": state,
                "amount_usd": entry.get("amount_usd"), "action": entry.get("action")}
        out.append(item)
        _tell(item)
    return out


def _tell(item: Dict[str, Any]) -> None:
    """One event per final state; a revert or an unknown outcome is a warning."""
    if item["state"] != "confirmed":
        logger.warning("money tx %s on %s: receipt %s%s", item["ref"], item["chain"],
                       item["state"], " — taken out of the daily cap"
                       if item["state"] == "reverted" else " after 24 h — check the chain")
    try:
        from core.event_kinds import TX_RECEIPT_RESOLVED
        from core.event_log import emit
        emit(TX_RECEIPT_RESOLVED, source="receipt_resolver", attrs=dict(item))
    except Exception:
        logger.debug("receipt resolver: event skipped", exc_info=True)
