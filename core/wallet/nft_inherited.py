"""What an NFT's account ALREADY holds when the NFT arrives — recorded once, with no ceremony.

Holding the NFT is the whole relation (proposal 115): no verb "adopts" it. When the holdings
watch (``core.wallet.nft_holdings``) sees an NFT of a pinned collection arrive on the wallet, this
module reads the account's balance of every token this instance already tracks on that chain
(the owner's token pins and the chain's canonical tokens) and records each one as an
``inherited`` position (``core.open_positions.seed_inherited``: no cost basis, the book reads it
NO_LEDGER; a row the rail already tracks is never overwritten).

An unreadable balance, an unreadable pin store or unknown decimals is NAMED and the row stays
unseeded, so the next pass tries again: unreadable is not empty. Reads only; signs nothing.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

Rpc = Callable[[str, list], Any]


def tracked_tokens(chain: str, rpc: Rpc) -> List[Dict[str, Any]]:
    """``[{address, symbol, decimals}]`` for *chain*: the owner's pins (an unreadable store
    RAISES) and the chain's canonical tokens. ``decimals`` is the pinned or frozen value, or
    None when this instance never read it."""
    from core.wallet import token_pins, tokens
    seen: Dict[str, Dict[str, Any]] = {}
    for row in token_pins.all_pins(chain=chain, strict=True):
        addr = str(row.get("address") or "").lower()
        if addr:
            seen[addr] = {"address": addr, "symbol": row.get("symbol") or None}
    for (c, addr), row in tokens._canonical_pins().items():
        if c == chain and str(addr).lower() not in seen:
            seen[str(addr).lower()] = {"address": str(addr).lower(), "symbol": row.get("symbol")}
    out = []
    for addr, row in seen.items():
        ident = tokens.get_token_identity(chain, addr, rpc=lambda m, p, _c=None: rpc(m, p))
        out.append({**row, "symbol": row["symbol"] or ident.symbol, "decimals": ident.decimals})
    return out


def seed(row: Dict[str, Any], *, rpc: Rpc, user_id: str,
         tokens_fn: Callable[[str, Rpc], List[Dict[str, Any]]] = None,
         db_path: str = None) -> Tuple[List[str], List[str]]:
    """Record the tracked tokens the account of *row* (a holdings-watch row) holds.
    Returns ``(held, unread)``: the symbols or addresses with a balance, and the reads that
    failed. ``unread`` empty means the pass was complete."""
    from core.open_positions import seed_inherited
    from core.wallet import abi
    chain, account = str(row["chain"]), str(row["account"])
    held: List[str] = []
    unread: List[str] = []
    try:
        listed = (tokens_fn or tracked_tokens)(chain, rpc)
    except Exception as exc:  # noqa: BLE001 — an unreadable token list is not an empty one
        return held, [f"the tracked token list ({type(exc).__name__}: {exc})"]
    for tok in listed:
        name = str(tok.get("symbol") or tok["address"])
        try:
            raw = rpc("eth_call", [{"to": tok["address"], "data": abi.encode_call(
                "balanceOf", [{"type": "address"}], [account])}, "latest"])
            balance = int(abi.decode([{"type": "uint256"}], raw)[0])
        except Exception as exc:  # noqa: BLE001
            unread.append(f"{name} ({type(exc).__name__})")
            continue
        if balance <= 0:
            continue
        decimals = tok.get("decimals")
        if decimals is None:
            unread.append(f"{name} (decimals unknown)")
            continue
        seed_inherited(user_id, chain=chain, account=account, address=tok["address"],
                       symbol=tok.get("symbol"), qty=balance / (10 ** int(decimals)), db_path=db_path)
        held.append(name)
    return held, unread
