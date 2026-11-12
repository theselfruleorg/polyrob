"""The open market orders this treasury signed (proposal 111 §8.3: "an order signature is
journaled like a transaction and listed by ``/nft`` (open orders) until filled, cancelled or
expired"; 115 W9a).

A signed listing is an obligation that lives OUTSIDE the chain until someone fills it, so the
instance keeps the list itself: ``<self tier>/agent_nft/orders.json`` (beside the holdings
watch's ``holdings.json``), one row per order the treasury signed through class O
(``agent_nft_sale_list``). The chain decides each row's state, never the file: :func:`status`
reads it live — the token moved (``transferCount``), the salt was used or cancelled, the nonce
moved (``incrementNonce``), the listing expired — and :func:`prune` (the holdings watch's tick)
drops a row once it has ended and says why, once. An unreadable read keeps the row ("unknown"
is never "ended"). An unreadable FILE raises (``core.state.files.UnreadableStateFile``): "I could
not read my orders" is never "I have none".

A listing the signer process holds for the owner's on-box approval (``sign.order`` answers
``approval_required``) is kept as ``pending`` (:func:`hold`): the retry after the approval must
sign the SAME listing (the same digest), so ``agent_nft_sale_list`` reuses it (:func:`held`)
instead of building a new one with a new salt and expiry.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

STATE_FILE = ("agent_nft", "orders.json")
#: Ended rows kept for the record (the newest).
ENDED_KEEP = 50

OPEN = "open"
EXPIRED = "expired"
CANCELLED = "cancelled"
MOVED = "filled or moved"
UNKNOWN = "unknown"


def path(home_dir=None, user_id: Optional[str] = None, instance_id: Optional[str] = None) -> Path:
    from core.instance import resolve_instance_id, resolve_owner_user_id, self_tier_root
    from core.runtime_paths import resolve_data_home
    home_dir = home_dir if home_dir is not None else resolve_data_home()
    root = Path(self_tier_root(home_dir, user_id or resolve_owner_user_id(),
                              instance_id or resolve_instance_id()))
    return root.joinpath(*STATE_FILE)


def _empty() -> Dict[str, Any]:
    return {"version": 1, "open": [], "ended": [], "pending": []}


def _same_token(r: Dict[str, Any], chain_id: int, collection: str, token_id: int) -> bool:
    return (int(r.get("chain_id") or 0) == int(chain_id)
            and str(r.get("collection")) == str(collection).lower()
            and int(r.get("token_id") or -1) == int(token_id))


def hold(row: Dict[str, Any], *, at: Optional[Path] = None) -> None:
    """Keep the ONE listing of a token that waits for the signer's on-box approval (``row``
    carries ``chain_id, collection, token_id, typed, promise, approval_id``); it replaces an
    older pending listing of the same token."""
    from core.state.files import locked_update_json

    def put(data):
        data["pending"] = [r for r in data.get("pending", []) if not _same_token(
            r, row["chain_id"], row["collection"], row["token_id"])] + [dict(row)]
    locked_update_json(str(at or path()), put, default=_empty)


def held(chain_id: int, collection: str, token_id: int, *,
         at: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The pending listing of this token, or None. Raises when the file is unreadable."""
    from core.state.files import read_json_strict
    data = read_json_strict(str(at or path()), _empty())
    return next((r for r in (data.get("pending") or [])
                 if _same_token(r, chain_id, collection, token_id)), None)


def release(chain_id: int, collection: str, token_id: int, *, at: Optional[Path] = None) -> None:
    """Drop the pending listing of this token (it was signed, or it can no longer be)."""
    from core.state.files import locked_update_json

    def drop(data):
        data["pending"] = [r for r in data.get("pending", [])
                           if not _same_token(r, chain_id, collection, token_id)]
    locked_update_json(str(at or path()), drop, default=_empty)


def record(row: Dict[str, Any], *, at: Optional[Path] = None) -> None:
    """Add one signed order (``chain, chain_id, collection, token_id, market, seller, salt,
    nonce, transfer_count, price, currency, expiry, digest, signed_at, link``)."""
    from core.state.files import locked_update_json

    def add(data):
        data.setdefault("open", []).append(dict(row))
    locked_update_json(str(at or path()), add, default=_empty)


def open_orders(*, at: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The open rows. Raises when the file exists and cannot be read."""
    from core.state.files import read_json_strict
    data = read_json_strict(str(at or path()), _empty())
    if not isinstance(data, dict) or not isinstance(data.get("open"), list):
        raise ValueError(f"{at or path()} is not an orders file")
    return list(data["open"])


def _word(raw) -> int:
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 66:
        raise ValueError(f"unreadable word {str(raw)[:20]!r}")
    return int(raw[2:66], 16)


def status(rpc, row: Dict[str, Any], *, now: Optional[int] = None) -> Tuple[str, str]:
    """``(state, why)`` of one order, read live. Never raises (a failed read is UNKNOWN)."""
    import core.wallet.sale_acts as A
    import core.wallet.sale_conditions as S
    try:
        market, seller = str(row["market"]), str(row["seller"])
        tc = _word(rpc("eth_call", [{"to": row["collection"],
                                     "data": S.encode_transfer_count(int(row["token_id"]))},
                                    "latest"]))
        if tc != int(row["transfer_count"]):
            return MOVED, f"the NFT moved since the listing (transferCount {row['transfer_count']} → {tc})"
        if A.read_used_salt(rpc, market, seller, int(row["salt"])):
            return CANCELLED, f"its salt {row['salt']} is used or cancelled"
        if A.read_nonce(rpc, market, seller) != int(row["nonce"]):
            return CANCELLED, "every order of this treasury was cancelled (the nonce moved)"
        if now is None:
            now = int((rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("timestamp"), 16)
        if int(now) > int(row["expiry"]):
            return EXPIRED, f"it expired at {row['expiry']}"
        return OPEN, ""
    except Exception as exc:  # noqa: BLE001 — unread is never "ended"
        return UNKNOWN, f"its state could not be read ({type(exc).__name__}: {exc})"


def line(row: Dict[str, Any], state: str, why: str) -> str:
    """One ``/nft`` line of an order."""
    unit = "wei (native)" if int(str(row.get("currency") or "0x0"), 16) == 0 else \
        f"raw units of {row.get('currency')}"
    return (f"LISTING salt {row.get('salt')}: {row.get('price')} {unit}, expires "
            f"{row.get('expiry')} — {state.upper()}" + (f" ({why})" if why else ""))


def prune(rpc_for: Callable[[str], Any], *, at: Optional[Path] = None
          ) -> List[Tuple[Dict[str, Any], str, str]]:
    """Drop every open row whose order has ENDED (moved, cancelled, expired), and return
    ``[(row, state, why)]`` of what was dropped. Unknown rows stay. Raises when unreadable."""
    from core.state.files import locked_update_json
    p = str(at or path())
    rows = open_orders(at=at)
    if not rows:
        return []
    ended: List[Tuple[Dict[str, Any], str, str]] = []
    for row in rows:
        try:
            state, why = status(rpc_for(row["chain"]), row)
        except Exception as exc:  # noqa: BLE001
            state, why = UNKNOWN, f"{type(exc).__name__}: {exc}"
        if state not in (OPEN, UNKNOWN):
            ended.append((row, state, why))
    if not ended:
        return []
    gone = {(r.get("chain_id"), r.get("market"), r.get("salt")) for r, _s, _w in ended}

    def drop(data):
        data["open"] = [r for r in data.get("open", [])
                        if (r.get("chain_id"), r.get("market"), r.get("salt")) not in gone]
        data["ended"] = (data.get("ended", []) + [dict(r, ended=s, why=w) for r, s, w in ended]
                         )[-ENDED_KEEP:]
    locked_update_json(p, drop, default=_empty)
    return ended


def ended_text(row: Dict[str, Any], state: str, why: str) -> str:
    """The owner's notice of an order that ended without a sale this watch saw."""
    return (f"My market listing of {row.get('collection')} #{row.get('token_id')} on "
            f"{row.get('chain')} (salt {row.get('salt')}) has ENDED: {state} — {why}. "
            + ("The market's approval of that token stays until a transfer clears it or "
               "agent_nft_sale_cancel_listing revokes it." if state == EXPIRED else ""))


__all__ = ["CANCELLED", "EXPIRED", "MOVED", "OPEN", "UNKNOWN", "ended_text", "held", "hold",
           "line", "open_orders", "path", "prune", "record", "release", "status"]
