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
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: SPL Token program (the classic one).
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

#: Token-2022. Enumerated by `token_holdings` (071 R3: the agent's own SPL
#: launches are Token-2022) and by `token_balances` (085 W8 L4: a Token-2022
#: holding is held — the guard, not this read, decides what may be spent).
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

SYSTEM_PROGRAM = "11111111111111111111111111111111"

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
    import time
    from core.wallet.rpc_response import read_response
    deadline = time.monotonic() + timeout
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(rpc_url(), data=body, headers={
        "content-type": "application/json", "user-agent": "polyrob-wallet/1.0", "accept-encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = read_response(r, deadline=deadline)
    if isinstance(payload, dict) and payload.get("error") is not None:
        raise RuntimeError(f"{method}: {payload['error']}")
    return (payload or {}).get("result")


def lamports_to_sol(lamports: Optional[int]) -> Optional[float]:
    return None if lamports is None else lamports / LAMPORTS_PER_SOL


def native_balance(address: str, *, rpc: Optional[Callable] = None) -> Optional[float]:
    """SOL balance, or ``None`` when it cannot be read. ``0.0`` is a real zero."""
    return lamports_to_sol(native_balance_raw(address, rpc=rpc))


def native_balance_raw(address: str, *, rpc: Optional[Callable] = None) -> Optional[int]:
    """SOL balance in LAMPORTS, or ``None`` when it cannot be read."""
    call = rpc or _rpc
    try:
        res = call("getBalance", [address])
        value = (res or {}).get("value")
        return None if value is None else int(value)
    except Exception as exc:
        logger.debug("solana: getBalance failed for %s (%s)", address, exc)
        return None


def token_balance_raw(address: str, mint: str, *,
                      rpc: Optional[Callable] = None) -> Optional[int]:
    """One classic-SPL balance in RAW units, SUMMED over every account the
    owner holds for ``mint``: ``0`` when the read succeeded and the owner holds
    none, ``None`` when the read failed or any account did not parse (an
    unparsed account could be the one holding the mint — unknown, not zero)."""
    call = rpc or _rpc
    try:
        res = call("getTokenAccountsByOwner",
                   [address, {"mint": mint}, {"encoding": "jsonParsed"}])
    except Exception as exc:
        logger.debug("solana: token balance read failed for %s (%s)", address, exc)
        return None
    total = 0
    for entry in ((res or {}).get("value") or []):
        try:
            info = entry["account"]["data"]["parsed"]["info"]
            if info["mint"] != mint:
                return None
            total += int(info["tokenAmount"]["amount"])
        except (KeyError, TypeError, ValueError):
            return None
    return total


def tx_inflow(signature: str, owner: str, mint: Optional[str] = None, *,
              rpc: Optional[Callable] = None) -> Optional[int]:
    """What ONE confirmed transaction paid ``owner``, in raw units: lamports
    when ``mint`` is None, else the summed classic-SPL balance change of the
    accounts ``owner`` owns for ``mint``. ``None`` when the transaction cannot
    be read, failed on-chain, or does not name ``owner`` at all.

    This is the exact arrival proof for a bridge INTO Solana: a balance rise
    cannot say whose rise it is when the same wallet trades, but the delivering
    transaction's own pre/post balances can.
    """
    got = tx_inflow_at(signature, owner, mint, rpc=rpc)
    return None if got is None else got[0]


def tx_inflow_at(signature: str, owner: str, mint: Optional[str] = None, *,
                 rpc: Optional[Callable] = None) -> Optional[tuple]:
    """:func:`tx_inflow` plus the transaction's ``blockTime``:
    ``(raw inflow, block time | None)``, or None (same cases as
    :func:`tx_inflow`). The block time lets a bridge refuse an EARLIER transfer
    offered as this one's delivery (085 B-1)."""
    call = rpc or _rpc
    try:
        tx = call("getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0}])
    except Exception as exc:
        logger.debug("solana: getTransaction failed for %s (%s)", signature, exc)
        return None
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    block_time = tx.get("blockTime")
    try:
        block_time = None if block_time is None else int(block_time)
    except (TypeError, ValueError):
        block_time = None
    try:
        if mint is None:
            keys = tx["transaction"]["message"]["accountKeys"]
            names = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
            if owner not in names:
                return None
            i = names.index(owner)
            return (int(meta["postBalances"][i]) - int(meta["preBalances"][i]),
                    block_time)

        def _sum(rows) -> int:
            return sum(int(r["uiTokenAmount"]["amount"]) for r in (rows or [])
                       if r.get("owner") == owner and r.get("mint") == mint
                       and r.get("programId", SPL_TOKEN_PROGRAM) == SPL_TOKEN_PROGRAM)
        post = meta.get("postTokenBalances")
        pre = meta.get("preTokenBalances")
        if post is None or pre is None:
            return None
        return _sum(post) - _sum(pre), block_time
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def mint_decimals(mint: str, *, rpc: Optional[Callable] = None) -> Optional[int]:
    """The mint's own ``decimals`` as the chain reports it, or ``None``."""
    call = rpc or _rpc
    try:
        res = call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        info = res["value"]["data"]["parsed"]["info"]
        if res["value"].get("owner") != SPL_TOKEN_PROGRAM:
            return None
        return int(info["decimals"])
    except Exception as exc:
        logger.debug("solana: mint decimals read failed for %s (%s)", mint, exc)
        return None


def token_balances(address: str, *,
                   rpc: Optional[Callable] = None) -> Optional[Dict[str, int]]:
    """``{mint: raw_amount}``, or ``None`` when the read failed.

    ``{}`` means "this wallet genuinely holds no SPL tokens" and ``None`` means
    "we could not look" — collapsing the two is how an outage gets reported as
    an empty wallet. Zero-balance accounts are dropped: a closed-out position
    leaves its (rent-funded) token account behind, and listing it would put a
    permanent phantom row in the portfolio.

    085 W8 L4: BOTH token programs are scanned. A Token-2022 holding that read
    as "not held" made the exit lanes refuse a real sell and under-reported the
    wallet. If either program's read fails, the whole read is UNKNOWN.
    """
    call = rpc or _rpc
    out: Dict[str, int] = {}
    seen: set = set()
    for program in (SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            res = call("getTokenAccountsByOwner",
                       [address, {"programId": program},
                        {"encoding": "jsonParsed"}])
        except Exception as exc:
            logger.debug("solana: getTokenAccountsByOwner(%s) failed for %s (%s)",
                         program, address, exc)
            return None
        for entry in ((res or {}).get("value") or []):
            try:
                pubkey = entry.get("pubkey")
                if pubkey is not None:
                    if pubkey in seen:
                        continue  # one account is counted once
                    seen.add(pubkey)
                info = entry["account"]["data"]["parsed"]["info"]
                mint = info["mint"]
                amount = int(info["tokenAmount"]["amount"])
            except (AttributeError, KeyError, TypeError, ValueError):
                # A shape we do not recognise is skipped, never guessed at — but the
                # rest of the wallet still reports.
                continue
            if amount:
                out[mint] = out.get(mint, 0) + amount
    return out


@dataclass(frozen=True)
class SplHolding:
    """One non-zero token account, as the chain reports it.

    ``decimals`` comes from the account's own ``tokenAmount`` (the mint's
    decimals, read by the RPC) — the caller never has to guess it, which is
    what used to render every unpinned mint as "raw units" (071 R2).
    """
    mint: str
    raw: int
    decimals: Optional[int]
    token_2022: bool = False
    extensions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SplHoldings:
    rows: List[SplHolding] = field(default_factory=list)
    #: False when the Token-2022 enumeration failed while the classic one
    #: succeeded: the rows are real, but the picture is PARTIAL.
    token_2022_read: bool = True


def _parse_holdings(res, *, token_2022: bool) -> List[SplHolding]:
    out: List[SplHolding] = []
    for entry in ((res or {}).get("value") or []):
        try:
            info = entry["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            amount = int(info["tokenAmount"]["amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if not amount:
            continue
        try:
            decimals: Optional[int] = int(info["tokenAmount"]["decimals"])
        except (KeyError, TypeError, ValueError):
            decimals = None
        exts = tuple(sorted(str(e.get("extension")) for e in (info.get("extensions") or [])
                            if isinstance(e, dict) and e.get("extension")))
        out.append(SplHolding(mint=mint, raw=amount, decimals=decimals,
                              token_2022=token_2022, extensions=exts))
    return out


def token_holdings(address: str, *, rpc: Optional[Callable] = None) -> Optional[SplHoldings]:
    """Every non-zero SPL and Token-2022 holding of ANY owner, with decimals.

    The DISPLAY read (portfolio, wallet_holdings). ``None`` = the classic read
    failed, so nothing can be claimed. A failed Token-2022 read keeps the
    classic rows and sets ``token_2022_read=False`` — partial, and said so.
    """
    call = rpc or _rpc
    try:
        classic = call("getTokenAccountsByOwner",
                       [address, {"programId": SPL_TOKEN_PROGRAM}, {"encoding": "jsonParsed"}])
    except Exception as exc:
        logger.debug("solana: classic token read failed for %s (%s)", address, exc)
        return None
    rows = _parse_holdings(classic, token_2022=False)
    t22_ok = True
    try:
        t22 = call("getTokenAccountsByOwner",
                   [address, {"programId": TOKEN_2022_PROGRAM}, {"encoding": "jsonParsed"}])
        rows += _parse_holdings(t22, token_2022=True)
    except Exception as exc:
        logger.debug("solana: token-2022 read failed for %s (%s)", address, exc)
        t22_ok = False
    # One owner can hold SEVERAL token accounts for the same mint (an ATA plus
    # auxiliary accounts); the holding is their sum, listed once.
    merged: Dict[Tuple[str, bool], SplHolding] = {}
    for h in rows:
        key = (h.mint, h.token_2022)
        prev = merged.get(key)
        merged[key] = h if prev is None else SplHolding(
            mint=h.mint, raw=prev.raw + h.raw,
            decimals=prev.decimals if prev.decimals is not None else h.decimals,
            token_2022=h.token_2022,
            extensions=tuple(sorted(set(prev.extensions) | set(h.extensions))))
    out = sorted(merged.values(), key=lambda h: h.mint)
    return SplHoldings(rows=out, token_2022_read=t22_ok)


def account_kind(address: str, *, rpc: Optional[Callable] = None) -> Optional[Dict[str, str]]:
    """What an address IS: ``{"kind": wallet|mint|token_account|program|other, ...}``.

    ``None`` = the read failed (unknown — callers proceed as before). A wallet
    is an account owned by the System program, or one that does not exist yet
    (an unfunded address is still a valid wallet address, never a mint).
    A token account also reports its ``mint`` and ``owner``.
    """
    call = rpc or _rpc
    try:
        res = call("getAccountInfo", [address, {"encoding": "jsonParsed"}])
    except Exception as exc:
        logger.debug("solana: getAccountInfo failed for %s (%s)", address, exc)
        return None
    value = (res or {}).get("value")
    if value is None:
        return {"kind": "wallet", "detail": "no account yet (unfunded)"}
    owner = value.get("owner")
    if value.get("executable"):
        return {"kind": "program"}
    if owner == SYSTEM_PROGRAM:
        return {"kind": "wallet"}
    if owner in (SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        parsed = (value.get("data") or {})
        parsed = parsed.get("parsed") if isinstance(parsed, dict) else None
        ptype = (parsed or {}).get("type")
        info = (parsed or {}).get("info") or {}
        if ptype == "mint":
            return {"kind": "mint"}
        if ptype == "account":
            return {"kind": "token_account", "mint": str(info.get("mint") or ""),
                    "owner": str(info.get("owner") or "")}
    return {"kind": "other", "owner": str(owner or "")}
