"""Best-effort Solana balance reads. Phase 2 — reads only, no rail.

Mirrors ``core/wallet/onchain.py``'s contract, including the part that matters
most: **a failed read is UNKNOWN (``None``), never a confident zero.** On Solana
that distinction is sharper than on EVM. An address with no token account and an
address whose RPC call failed look identical if you collapse both to an empty
mapping, and only one of them means "you hold none of this".

Parity note from the Solana research: token enumeration here needs no indexer
key. ``getTokenAccountsByOwner`` returns every SPL balance the owner holds
directly from the RPC, where the EVM side needs Alchemy to enumerate at all.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: SPL Token program. The classic one; Token-2022 accounts live under a
#: different program id and are deliberately NOT enumerated here — their
#: transfer-hook and fee extensions change what a balance even means, and
#: reporting them as ordinary SPL would overstate what is spendable.
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

LAMPORTS_PER_SOL = 1_000_000_000


def rpc_url() -> str:
    """Operator-pinnable endpoint, else the registry's public fallback."""
    pinned = os.getenv("DEFI_SOLANA_RPC", "").strip()
    if pinned:
        return pinned
    from core.wallet import chains
    row = chains.get("solana")
    return row.public_rpc if row else ""


def _rpc(method: str, params: list, timeout: float = 8.0):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(rpc_url(), data=body, headers={
        "content-type": "application/json", "user-agent": "polyrob-wallet/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    if isinstance(payload, dict) and payload.get("error") is not None:
        raise RuntimeError(f"{method}: {payload['error']}")
    return (payload or {}).get("result")


def lamports_to_sol(lamports: Optional[int]) -> Optional[float]:
    return None if lamports is None else lamports / LAMPORTS_PER_SOL


def native_balance(address: str, *, rpc: Optional[Callable] = None) -> Optional[float]:
    """SOL balance, or ``None`` when it cannot be read. ``0.0`` is a real zero."""
    call = rpc or _rpc
    try:
        res = call("getBalance", [address])
    except Exception as exc:
        logger.debug("solana: getBalance failed for %s (%s)", address, exc)
        return None
    value = (res or {}).get("value")
    return None if value is None else lamports_to_sol(int(value))


def token_balances(address: str, *,
                   rpc: Optional[Callable] = None) -> Optional[Dict[str, int]]:
    """``{mint: raw_amount}``, or ``None`` when the read failed.

    ``{}`` means "this wallet genuinely holds no SPL tokens" and ``None`` means
    "we could not look" — collapsing the two is how an outage gets reported as
    an empty wallet. Zero-balance accounts are dropped: a closed-out position
    leaves its (rent-funded) token account behind, and listing it would put a
    permanent phantom row in the portfolio.
    """
    call = rpc or _rpc
    try:
        res = call("getTokenAccountsByOwner",
                   [address, {"programId": SPL_TOKEN_PROGRAM},
                    {"encoding": "jsonParsed"}])
    except Exception as exc:
        logger.debug("solana: getTokenAccountsByOwner failed for %s (%s)", address, exc)
        return None
    out: Dict[str, int] = {}
    for entry in ((res or {}).get("value") or []):
        try:
            info = entry["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            amount = int(info["tokenAmount"]["amount"])
        except (KeyError, TypeError, ValueError):
            # A shape we do not recognise is skipped, never guessed at — but the
            # rest of the wallet still reports.
            continue
        if amount:
            out[mint] = amount
    return out
