"""On-chain USDC settlement detection probe (Task 11, Phase 2) — the
"de-Coinbase" move: today an agent invoice settles ONLY via owner attestation
or a payer-driven facilitator `POST /pay`. A human payer who just sends USDC
straight to the treasury address (exactly what the text/QR instructions
invite) is never detected. This module gives the settlement watcher
(`modules/x402/settlement_watcher.py`) a way to SEE that transfer directly
on-chain — no facilitator required.

Pure, read-only, no signing. `scan_treasury_transfers`/`get_head_block` take
an INJECTED ``rpc`` callable — ``rpc(method: str, params: list) -> Any``
returning the JSON-RPC ``result`` field — rather than owning a URL/HTTP
client themselves, so they are trivially mockable in tests (no real chain,
no network) and so the watcher can wire the SAME RPC helper + USDC contract
constants `core/wallet/onchain.py` already trusts for the agent's own
balance reads, instead of a second copy that could drift (mirrors the T4b
asset-pin reasoning).

Fail-open by design: any RPC or shape error returns ``[]``/``None`` — "no
detection this tick" — and never raises into the watcher's tick.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# rpc(method, params) -> JSON-RPC `result` (already unwrapped).
RpcCall = Callable[[str, list], Any]

# keccak256("Transfer(address,address,uint256)") — the standard ERC-20
# Transfer event topic0. A well-known constant; no eth-hash/web3 dependency
# is needed to compute it at runtime.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# USDC is always 6 decimals — the treasury only ever expects canonical USDC
# (same asset-pin reasoning tools/x402/real_client.py already applies on the
# paying side), so this is a safe constant, not an assumption per-transfer.
_USDC_DECIMALS = 6


def _pad_address_topic(address: str) -> str:
    """A 20-byte address left-padded to a 32-byte log topic."""
    return "0x" + address.strip().lower().replace("0x", "").rjust(64, "0")


def _topic_to_address(topic: Any) -> str:
    return "0x" + str(topic or "")[-40:]


def get_head_block(rpc: RpcCall) -> Optional[int]:
    """Latest block number via `eth_blockNumber`, or None on any failure
    (fail-open — the caller treats None as "skip this tick")."""
    try:
        raw = rpc("eth_blockNumber", [])
        if not raw:
            return None
        return int(raw, 16)
    except Exception:
        logger.debug("onchain_probe: eth_blockNumber failed", exc_info=True)
        return None


def scan_treasury_transfers(
    rpc: RpcCall,
    usdc_addr: str,
    treasury: str,
    from_block: int,
    to_block: int,
) -> List[Dict[str, Any]]:
    """USDC ``Transfer(from, to=treasury, value)`` logs in
    ``[from_block, to_block]`` (inclusive), fetched via `eth_getLogs`
    filtered server-side on the `to` topic — only transfers INTO the
    treasury are ever returned.

    Returns ``[{tx_hash, from, amount_usd, block}, ...]``; ``amount_usd`` is
    ``value / 10**6`` rounded to 6 decimals (USDC's own precision) so it
    compares exactly against the ``amount_usd`` REAL column on the invoice
    table. Any RPC or malformed-log error is swallowed — this returns ``[]``
    rather than ever raising into the watcher's tick.
    """
    if from_block > to_block:
        return []
    try:
        params = [{
            "fromBlock": hex(int(from_block)),
            "toBlock": hex(int(to_block)),
            "address": usdc_addr,
            "topics": [TRANSFER_TOPIC, None, _pad_address_topic(treasury)],
        }]
        logs = rpc("eth_getLogs", params)
    except Exception:
        logger.warning(
            "onchain_probe: eth_getLogs failed (blocks %s..%s)",
            from_block, to_block, exc_info=True)
        return []
    if not logs:
        return []
    out: List[Dict[str, Any]] = []
    for log in logs:
        try:
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            value = int(log.get("data") or "0x0", 16)
            out.append({
                "tx_hash": log.get("transactionHash"),
                "from": _topic_to_address(topics[1]),
                "amount_usd": round(value / (10 ** _USDC_DECIMALS), _USDC_DECIMALS),
                "block": int(log.get("blockNumber") or "0x0", 16),
            })
        except Exception:
            logger.debug("onchain_probe: skipping malformed log entry", exc_info=True)
            continue
    return out
