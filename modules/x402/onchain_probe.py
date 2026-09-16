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

#: The decimals a caller gets when it does not say. 6 = USDC, which is what
#: every pre-046 caller meant.
#:
#: ⚠️ It is a DEFAULT, not an assumption. An asset row
#: (`core/payments/assets.py`) is the authority, and a caller that has one MUST
#: pass it — an 18-decimal value divided by 10**6 is not a price, it is a
#: number a thousand billion times too large.
DEFAULT_DECIMALS = 6

#: Back-compat alias for the pre-046 name.
_USDC_DECIMALS = DEFAULT_DECIMALS


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
    token_addr: str,
    treasury: str,
    from_block: int,
    to_block: int,
    *,
    decimals: int = DEFAULT_DECIMALS,
) -> Optional[List[Dict[str, Any]]]:
    """ERC-20 ``Transfer(from, to=treasury, value)`` logs for ONE token in
    ``[from_block, to_block]`` (inclusive), fetched via `eth_getLogs`
    filtered server-side on the `to` topic — only transfers INTO the
    treasury are ever returned.

    Returns ``[{tx_hash, from, amount_raw, amount_usd, block}, ...]``.

    ⚠️ ``amount_raw`` is the EXACT integer from the log and is what settlement
    matches on. ``amount_usd`` is ``value / 10**decimals`` and is for DISPLAY
    only. Matching on the float was safe only while every asset had 6 decimals;
    an 18-decimal value does not survive the round trip (proposal 046 §4.3).

    **Return contract (audit 2026-08-07 #1).** ``None`` means the range was NOT
    scanned (RPC error/timeout) — the caller MUST NOT advance its checkpoint
    past it. ``[]`` means the range was scanned and genuinely held no transfers.
    Conflating the two silently burned block ranges containing real payments,
    unrecoverably, because ``advance_scan_checkpoint`` refuses to regress. Still
    never raises into the watcher's tick.
    """
    if from_block > to_block:
        return []
    try:
        params = [{
            "fromBlock": hex(int(from_block)),
            "toBlock": hex(int(to_block)),
            "address": token_addr,
            "topics": [TRANSFER_TOPIC, None, _pad_address_topic(treasury)],
        }]
        logs = rpc("eth_getLogs", params)
    except Exception:
        logger.warning(
            "onchain_probe: eth_getLogs FAILED (blocks %s..%s) — range NOT scanned; "
            "the settlement checkpoint must not advance past it",
            from_block, to_block, exc_info=True)
        return None
    if logs is None:
        # Defensive: a caller-supplied rpc that yields None is not an empty range.
        logger.warning(
            "onchain_probe: eth_getLogs returned no result (blocks %s..%s) — treating as UNSCANNED",
            from_block, to_block)
        return None
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
                "amount_raw": value,
                "amount_usd": round(value / (10 ** int(decimals)), 6),
                "block": int(log.get("blockNumber") or "0x0", 16),
            })
        except Exception:
            logger.debug("onchain_probe: skipping malformed log entry", exc_info=True)
            continue
    return out
