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

#: SPL Token program. The classic one; Token-2022 accounts live under a
#: different program id and are deliberately NOT enumerated here — their
#: transfer-hook and fee extensions change what a balance even means, and
#: reporting them as ordinary SPL would overstate what is spendable.
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

#: Token-2022. Enumerated ONLY by the display read (`token_holdings`), never by
#: `token_balances`, which spend-side callers use to decide what is spendable.
#: The agent's own SPL launches are Token-2022 (`spl_token.py`), so a display
#: read that skipped this program could not see the agent's own token (071 R3).
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
