"""Solana mint FACTS straight from the chain (071 W2 §3.6) — no provider, no key.

What a screener tells you about a Solana token, the RPC can tell you first-hand:
who can still mint, who can freeze, which Token-2022 extensions the mint carries,
who holds the supply, and whether a pump.fun bonding curve still owns it. These
are facts, not scores, so they are read here and judged by the caller.

Every reader returns ``None`` (or raises) when it could not read — an unread
authority is UNKNOWN, never "revoked". A parsed shape this module does not
recognise is reported as such, never guessed at.

Pure parsers (``parse_mint``, ``parse_pump_curve``, ``holder_shares``) carry the
logic and are unit-tested on recorded payloads; the ``read_*`` functions are the
thin network boundary over :func:`core.wallet.solana_onchain._rpc`.
"""
from __future__ import annotations

import base64
import logging
import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.wallet.solana_onchain import SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM

logger = logging.getLogger(__name__)

#: pump.fun's bonding-curve program. A mint launched there is owned by a curve
#: PDA (seeds ``["bonding-curve", mint]``) until the curve completes and the
#: liquidity migrates to an AMM.
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
#: Anchor discriminator of the ``BondingCurve`` account. Verified against the
#: live chain on 2026-10-02 (an on-curve mint and a graduated one): an account at
#: the PDA that does not start with these bytes is NOT a curve — the USDC PDA,
#: for instance, exists as an empty System account somebody sent lamports to.
PUMP_CURVE_DISCRIMINATOR = bytes.fromhex("17b7f83760d8ac60")
#: Tokens a fresh pump.fun curve starts with for SALE (793.1M of the 1B supply
#: at 6 decimals); the rest is reserved for the migration pool. Observed on the
#: live chain 2026-10-02 (a just-launched curve's ``real_token_reserves``). A
#: curve that started from a different figure makes the sold fraction an
#: approximation, which is why the raw reserves are carried too.
PUMP_INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000

#: Token-2022 extensions this module KNOWS how to judge. Any other extension on a
#: mint is listed as UNASSESSED — an extension we cannot read is not a benign one.
KNOWN_EXTENSIONS = frozenset({
    "permanentDelegate", "transferHook", "transferFeeConfig", "nonTransferable",
    "defaultAccountState", "mintCloseAuthority", "confidentialTransferMint",
    "confidentialTransferFeeConfig", "metadataPointer", "tokenMetadata",
    "interestBearingConfig", "pausableConfig", "scaledUiAmountConfig",
    "groupPointer", "groupMemberPointer", "tokenGroup", "tokenGroupMember",
})


@dataclass(frozen=True)
class MintFacts:
    """One mint as the chain states it. ``None`` on a field = not stated."""
    mint: str
    program: str                     # "spl-token" | "spl-token-2022"
    decimals: Optional[int]
    supply_raw: Optional[int]
    mint_authority: Optional[str]    # None = revoked (the chain says null)
    freeze_authority: Optional[str]  # None = revoked
    #: extension name -> its parsed ``state`` dict ({} when it has none).
    extensions: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def token_2022(self) -> bool:
        return self.program == "spl-token-2022"

    @property
    def supply_human(self) -> Optional[float]:
        if self.supply_raw is None or self.decimals is None:
            return None
        return self.supply_raw / (10 ** self.decimals)

    @property
    def unassessed_extensions(self) -> List[str]:
        return sorted(e for e in self.extensions if e not in KNOWN_EXTENSIONS)


class NotAMint(ValueError):
    """The address exists but is not a token mint (or does not exist)."""


def parse_mint(mint: str, value: Optional[Dict[str, Any]]) -> MintFacts:
    """Pure. ``value`` is ``getAccountInfo(jsonParsed).value``.

    Raises :class:`NotAMint` when the account is absent or not a mint, and
    ``ValueError`` on a shape it does not recognise — never a guessed answer.
    """
    if value is None:
        raise NotAMint("no account at this address")
    owner = value.get("owner")
    if owner not in (SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise NotAMint(f"account is owned by {owner}, not a token program")
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict):
        raise ValueError("the RPC did not return a parsed mint")
    if parsed.get("type") != "mint":
        raise NotAMint(f"token-program account of type {parsed.get('type')!r}, not a mint")
    info = parsed.get("info")
    if not isinstance(info, dict) or "mintAuthority" not in info or "freezeAuthority" not in info:
        # Both keys are always present (null when revoked). Absent = a shape we
        # do not know, and an absent authority must never read as revoked.
        raise ValueError("parsed mint carries no authority fields")
    try:
        decimals: Optional[int] = int(info.get("decimals"))
    except (TypeError, ValueError):
        decimals = None
    try:
        supply: Optional[int] = int(str(info.get("supply")))
    except (TypeError, ValueError):
        supply = None
    exts: Dict[str, Dict[str, Any]] = {}
    for item in info.get("extensions") or []:
        if isinstance(item, dict) and item.get("extension"):
            state = item.get("state")
            exts[str(item["extension"])] = state if isinstance(state, dict) else {}
    program = "spl-token-2022" if owner == TOKEN_2022_PROGRAM else "spl-token"
    return MintFacts(
        mint=mint, program=program, decimals=decimals, supply_raw=supply,
        mint_authority=info.get("mintAuthority") or None,
        freeze_authority=info.get("freezeAuthority") or None,
        extensions=exts)


@dataclass(frozen=True)
class HolderLine:
    token_account: str
    owner: Optional[str]          # None = the owner could not be resolved
    amount_raw: int
    share: Optional[float]        # FRACTION of supply; None when supply unknown
    #: True = the owner is a program-derived address (off the ed25519 curve):
    #: a pool vault, a bonding curve, a lock or a multisig — not a key a person
    #: holds. None = could not be decided.
    program_owned: Optional[bool] = None
    label: Optional[str] = None


def _off_curve(address: str) -> Optional[bool]:
    try:
        from solders.pubkey import Pubkey  # type: ignore
    except Exception:
        return None
    try:
        return not Pubkey.from_string(address).is_on_curve()
    except Exception:
        return None


def holder_shares(largest: Optional[Dict[str, Any]],
                  owners: Optional[Dict[str, Any]],
                  supply_raw: Optional[int], *,
                  labels: Optional[Dict[str, str]] = None) -> List[HolderLine]:
    """Pure. ``largest`` = ``getTokenLargestAccounts`` result, ``owners`` =
    ``getMultipleAccounts(jsonParsed)`` result over the same accounts, in order.

    A zero-amount account is dropped (a closed-out holder is not a holder). A
    missing supply leaves every share ``None`` — unknown, never 0 %.
    """
    rows = (largest or {}).get("value") or []
    owner_vals = (owners or {}).get("value") or []
    out: List[HolderLine] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        acct = str(row.get("address") or "")
        try:
            amount = int(str(row.get("amount")))
        except (TypeError, ValueError):
            continue
        if not acct or amount <= 0:
            continue
        owner = None
        if i < len(owner_vals) and isinstance(owner_vals[i], dict):
            try:
                owner = owner_vals[i]["data"]["parsed"]["info"]["owner"] or None
            except (KeyError, TypeError):
                owner = None
        share = (amount / supply_raw) if supply_raw else None
        label = (labels or {}).get(owner or "") or (labels or {}).get(acct)
        out.append(HolderLine(
            token_account=acct, owner=owner, amount_raw=amount, share=share,
            program_owned=_off_curve(owner) if owner else None, label=label))
    return out


@dataclass(frozen=True)
class PumpCurve:
    """pump.fun bonding-curve state. ``state``: on_curve | graduated | not_pump."""
    state: str
    curve_address: Optional[str] = None
    virtual_token_reserves: Optional[int] = None
    virtual_sol_reserves: Optional[int] = None
    real_token_reserves: Optional[int] = None
    real_sol_reserves: Optional[int] = None
    token_total_supply: Optional[int] = None
    creator: Optional[str] = None

    @property
    def sold_fraction(self) -> Optional[float]:
        """Share of the curve's sale allocation already bought (≈ progress to
        graduation). Approximate by construction — see the constant's note."""
        if self.state != "on_curve" or self.real_token_reserves is None:
            return None
        left = min(self.real_token_reserves, PUMP_INITIAL_REAL_TOKEN_RESERVES)
        return 1.0 - left / PUMP_INITIAL_REAL_TOKEN_RESERVES

    @property
    def sol_in_curve(self) -> Optional[float]:
        return None if self.real_sol_reserves is None else self.real_sol_reserves / 1e9


def pump_curve_address(mint: str) -> Optional[str]:
    """The curve PDA for *mint*, or None when ``solders`` is unavailable."""
    try:
        from solders.pubkey import Pubkey  # type: ignore
        pda, _bump = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(PUMP_FUN_PROGRAM))
        return str(pda)
    except Exception:
        return None


def parse_pump_curve(curve_address: str, value: Optional[Dict[str, Any]]) -> PumpCurve:
    """Pure. ``value`` = ``getAccountInfo(base64).value`` of the curve PDA.

    Layout (Anchor, little-endian, verified live 2026-10-02): 8-byte
    discriminator, five u64 (virtual token, virtual SOL, real token, real SOL,
    token total supply), one bool ``complete``, then the 32-byte creator.
    """
    if value is None or value.get("owner") != PUMP_FUN_PROGRAM:
        return PumpCurve(state="not_pump")
    data = value.get("data")
    raw = data[0] if isinstance(data, list) and data else None
    if not isinstance(raw, str):
        raise ValueError("curve account data is not base64")
    blob = base64.b64decode(raw)
    if len(blob) < 49 or blob[:8] != PUMP_CURVE_DISCRIMINATOR:
        raise ValueError("account at the curve address is not a bonding curve")
    vt, vs, rt, rs, total = struct.unpack_from("<5Q", blob, 8)
    complete = blob[48] == 1
    creator = None
    if len(blob) >= 81:
        try:
            from solders.pubkey import Pubkey  # type: ignore
            c = str(Pubkey.from_bytes(blob[49:81]))
            creator = None if c == "11111111111111111111111111111111" else c
        except Exception:
            creator = None
    return PumpCurve(
        state="graduated" if complete else "on_curve", curve_address=curve_address,
        virtual_token_reserves=vt, virtual_sol_reserves=vs,
        real_token_reserves=rt, real_sol_reserves=rs, token_total_supply=total,
        creator=creator)


# --------------------------------------------------------------------------
# Network boundary — blocked in unit tests (tests/unit/tools/defi/conftest.py).
# --------------------------------------------------------------------------

def _rpc(method: str, params: list, timeout: float = 8.0):
    from core.wallet.solana_onchain import _rpc as call
    return call(method, params, timeout)


def read_mint(mint: str, *, rpc: Optional[Callable] = None) -> MintFacts:
    """Raises on a failed read (the caller names the failure)."""
    res = (rpc or _rpc)("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    return parse_mint(mint, (res or {}).get("value"))


def read_holders(mint: str, supply_raw: Optional[int], *,
                 rpc: Optional[Callable] = None,
                 labels: Optional[Dict[str, str]] = None) -> List[HolderLine]:
    """Top-20 holders (the RPC's own cap). Raises on a failed read.

    ⚠️ The public mainnet endpoint rate-limits ``getTokenLargestAccounts`` per
    method (HTTP-429 measured 2026-10-02 on every attempt); a keyed endpoint
    pinned in ``DEFI_SOLANA_RPC`` is what makes this read reliable.
    """
    call = rpc or _rpc
    largest = call("getTokenLargestAccounts", [mint])
    accounts = [str(r.get("address")) for r in ((largest or {}).get("value") or [])
                if isinstance(r, dict) and r.get("address")]
    owners = None
    if accounts:
        try:
            owners = call("getMultipleAccounts", [accounts, {"encoding": "jsonParsed"}])
        except Exception as exc:
            # Rows without owners are still true rows; the owner reads unknown.
            logger.debug("solana: owner resolution failed for %s (%s)", mint, exc)
    return holder_shares(largest, owners, supply_raw, labels=labels)


def read_pump_curve(mint: str, *, rpc: Optional[Callable] = None) -> Optional[PumpCurve]:
    """The curve state, or None when the PDA could not be derived. Raises on a
    failed read."""
    addr = pump_curve_address(mint)
    if addr is None:
        return None
    res = (rpc or _rpc)("getAccountInfo", [addr, {"encoding": "base64"}])
    return parse_pump_curve(addr, (res or {}).get("value"))


def concentration(rows: List[HolderLine]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(top-1 share, top-10 share, top-10 share EXCLUDING program-owned rows).

    ``None`` when no row carries a share. Program-owned rows (pools, curves,
    locks, multisigs) are excluded from the third figure because a pool vault
    is not a person who can dump — but they are NAMED, never silently dropped.
    """
    shares = [r.share for r in rows if r.share is not None]
    if not shares:
        return None, None, None
    top1 = max(shares)
    top10 = sum(sorted(shares, reverse=True)[:10])
    people = sorted((r.share for r in rows
                     if r.share is not None and r.program_owned is not True), reverse=True)
    return top1, top10, sum(people[:10]) if people else 0.0
