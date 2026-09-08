"""Protocol-level token identity for the DeFi read tier.

⚠️ TRUST NOTE — read before extending.

``symbol()``, ``name()`` and ``decimals()`` are chosen by the token contract, so
they are **attacker-authored**. Reading them over RPC does not make them true:
a hostile token can return different values on different calls, omit them
entirely (all three are optional in ERC-20), or change them via a proxy upgrade.

The trust line is NOT core-vs-tools. It is:

  * **protocol-level fact** — a balance held at a given contract, block data.
    The chain enforces these.
  * **contract-self-reported metadata** — symbol/name/decimals. Claims only.

``decimals`` converts raw units to human amounts, so it denominates every USD
valuation here and (at proposal-023 T3) the ``min_receive`` and delta
assertions the whole security model rests on. A token reporting 6 at simulation
and 18 at execution would break that model. Therefore:

  1. Canonical tokens take decimals from the pinned list; the chain is never
     consulted for them.
  2. Everything else is FROZEN on first sight. A later differing read is not an
     update — it surfaces as ``metadata_changed`` and the token stays unverified.
  3. Money math reads the pinned/frozen value, never a fresh one.

A token is identified by ``(chain, address)``. There is deliberately **no**
symbol->address lookup in this module: binding a ticker to a contract is the
primary injection surface (proposal 023 §2.3.4), and it lives in the tools tier
as an explicitly-lossy discovery aid that returns candidates and never picks.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from eth_utils import is_hex_address, to_checksum_address

from core import sqlite_util
from core.wallet import onchain

logger = logging.getLogger(__name__)

# ERC-20 view selectors.
_SEL_DECIMALS = "0x313ce567"
_SEL_SYMBOL = "0x95d89b41"
_SEL_NAME = "0x06fdde03"

#: Pinned, operator-curated identities. Presence here is the ONLY source of
#: ``verified=True`` — verification is never inferred from liquidity, which is
#: purchasable. USDC reuses the constant ``core/wallet/onchain.py`` already
#: trusts (and ``tools/x402/real_client.py`` asset-pins against) so there is no
#: second copy to drift.
def _canonical_pins() -> Dict[Tuple[str, str], Dict[str, object]]:
    """USDC/wrapped-native pins for every chain the registry has VERIFIED.

    A row contributes when its ADDRESSES were checked on-chain — ``eth_getCode``
    plus symbol/decimals, or the non-EVM equivalent. ``money_enabled`` implies
    that (arming a chain required checking it first); ``assets_verified`` states
    it for a row that is verified WITHOUT moving value through the EVM rail.
    Solana is exactly that case: its money goes through ``solana_swap``, so it
    is not ``money_enabled``, yet its USDC mint is the same constant
    ``solana_x402.py`` pins the live settlement rail against.

    A chain whose addresses are carried for balance reads only contributes
    nothing here — it would claim a verification nobody performed, and
    ``verified=True`` is the one thing this table is the source of.
    """
    from core.wallet import chains
    pins: Dict[Tuple[str, str], Dict[str, object]] = {}
    for row in chains.all_rows():
        if not (row.money_enabled or row.assets_verified):
            continue
        if row.usdc:
            pins[(row.name, row.usdc)] = {
                "symbol": "USDC", "name": "USD Coin", "decimals": 6}
        if row.wrapped_native:
            # The NAME is derived, never hardcoded: Polygon's wrapped native is
            # WPOL, and labelling it "Wrapped Ether" would put a false identity
            # on the canonical list — the one table whose entire value is that
            # `verified=True` means somebody checked.
            # Decimals come from the ROW, never a hardcoded 18: wSOL is 9, and
            # an 18 here would size a wSOL amount a billion times wrong.
            pins[(row.name, row.wrapped_native)] = {
                "symbol": f"W{row.native_symbol}",
                "name": f"Wrapped {row.native_symbol}",
                "decimals": row.native_decimals}
    return pins


CANONICAL_TOKENS: Dict[Tuple[str, str], Dict[str, object]] = _canonical_pins()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    chain         TEXT NOT NULL,
    address       TEXT NOT NULL,
    symbol        TEXT,
    name          TEXT,
    decimals      INTEGER,
    first_seen_ts REAL NOT NULL,
    change_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chain, address)
)
"""


@dataclass(frozen=True)
class TokenIdentity:
    """Identity of ``(chain, address)``.

    ``decimals is None`` means UNKNOWN (the contract has no ``decimals()`` or the
    read failed) — never assume 0. ``metadata_changed`` means the contract now
    reports something different from what was frozen on first sight; the frozen
    value is what this carries.
    """
    chain: str
    address: str
    symbol: Optional[str]
    name: Optional[str]
    decimals: Optional[int]
    verified: bool
    metadata_changed: bool
    source: str  # "canonical" | "frozen" | "first_seen"

    @property
    def valuable(self) -> bool:
        """True when this token can be converted to a human amount at all."""
        return self.decimals is not None


def normalize_address(addr: str) -> str:
    """Return the EIP-55 checksummed form, or raise ``ValueError``.

    A mixed-case address whose checksum FAILS is refused, never silently
    lowercased. That checksum is the only typo detector Ethereum has built in,
    and discarding it becomes a fund-loss vector the moment a money verb exists.
    An all-lowercase or all-uppercase address carries no checksum information,
    so it is accepted and checksummed.
    """
    if not isinstance(addr, str) or not is_hex_address(addr):
        raise ValueError(f"not a 20-byte hex address: {addr!r}")
    body = addr[2:] if addr.startswith("0x") else addr
    mixed = body != body.lower() and body != body.upper()
    checksummed = to_checksum_address("0x" + body.lower())
    if mixed and checksummed != ("0x" + body):
        raise ValueError(
            f"EIP-55 checksum failed for {addr!r} — refusing rather than "
            "normalizing (a failed checksum usually means a typo or a swapped "
            "address). If you are sure of the address, pass it ALL-LOWERCASE: a "
            "lowercase address carries no checksum and is accepted as-is.")
    return checksummed


def canonical_token(chain: str, address: str) -> Optional[Dict[str, object]]:
    """Pinned identity for ``(chain, address)``, or None."""
    return CANONICAL_TOKENS.get((chain, address))


def _db(db_path: Optional[str]) -> str:
    if db_path:
        return db_path
    from core.runtime_paths import sidecar_db_path
    return str(sidecar_db_path("defi_tokens.db"))


def _ensure_schema(path: str) -> None:
    conn = sqlite_util.wal_connect(path)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _decode_uint(raw: Optional[str]) -> Optional[int]:
    if not raw or raw == "0x":
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def _decode_string(raw: Optional[str]) -> Optional[str]:
    """Decode an ABI-encoded dynamic string, tolerating bytes32-style returns."""
    if not raw or raw == "0x":
        return None
    body = raw[2:]
    try:
        data = bytes.fromhex(body)
    except ValueError:
        return None
    if len(data) >= 64:
        offset = int.from_bytes(data[:32], "big")
        if offset == 32 and len(data) >= 64:
            length = int.from_bytes(data[32:64], "big")
            chunk = data[64:64 + length]
            text = chunk.decode("utf-8", errors="replace").strip("\x00")
            return text or None
    # bytes32-style: right-padded raw text
    text = data.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
    return text or None


def _default_rpc(method: str, params: list, chain: str):
    return onchain._rpc(onchain.rpc_url_for_chain(chain), method, params, 4.0)


def _read_chain_metadata(chain: str, address: str, rpc: Callable):
    """(symbol, name, decimals) as the contract currently claims. Any field may
    be None — all three are optional in ERC-20 and any may revert."""
    def call(selector):
        try:
            return rpc("eth_call", [{"to": address, "data": selector}, "latest"], chain)
        except Exception:
            return None

    return (_decode_string(call(_SEL_SYMBOL)),
            _decode_string(call(_SEL_NAME)),
            _decode_uint(call(_SEL_DECIMALS)))


def get_token_identity(
    chain: str,
    address: str,
    *,
    db_path: Optional[str] = None,
    rpc: Optional[Callable] = None,
) -> TokenIdentity:
    """Identity for ``(chain, address)``. Never raises on a chain failure;
    raises ``ValueError`` on a malformed address (validated before any RPC)."""
    address = normalize_address(address)

    pinned = canonical_token(chain, address)
    if pinned is not None:
        return TokenIdentity(
            chain=chain, address=address,
            symbol=str(pinned["symbol"]), name=str(pinned["name"]),
            decimals=int(pinned["decimals"]),  # type: ignore[arg-type]
            verified=True, metadata_changed=False, source="canonical")

    rpc = rpc or _default_rpc
    path = _db(db_path)
    _ensure_schema(path)

    conn = sqlite_util.wal_connect(path)
    try:
        row = conn.execute(
            "SELECT symbol, name, decimals, change_count FROM tokens "
            "WHERE chain = ? AND address = ?", (chain, address)).fetchone()

        fresh_symbol, fresh_name, fresh_decimals = _read_chain_metadata(chain, address, rpc)

        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO tokens "
                "(chain, address, symbol, name, decimals, first_seen_ts, change_count) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (chain, address, fresh_symbol, fresh_name, fresh_decimals, time.time()))
            conn.commit()
            return TokenIdentity(
                chain=chain, address=address, symbol=fresh_symbol, name=fresh_name,
                decimals=fresh_decimals, verified=False, metadata_changed=False,
                source="first_seen")

        frozen_symbol, frozen_name, frozen_decimals, change_count = row
        changed = any((
            fresh_decimals is not None and fresh_decimals != frozen_decimals,
            fresh_symbol is not None and fresh_symbol != frozen_symbol,
            fresh_name is not None and fresh_name != frozen_name,
        ))
        if changed:
            conn.execute(
                "UPDATE tokens SET change_count = change_count + 1 "
                "WHERE chain = ? AND address = ?", (chain, address))
            conn.commit()
            logger.warning(
                "token metadata MUTATED for %s/%s — frozen(symbol=%r decimals=%r) "
                "now reports(symbol=%r decimals=%r); keeping the frozen values",
                chain, address, frozen_symbol, frozen_decimals, fresh_symbol, fresh_decimals)

        return TokenIdentity(
            chain=chain, address=address, symbol=frozen_symbol, name=frozen_name,
            decimals=frozen_decimals, verified=False,
            metadata_changed=bool(changed or change_count), source="frozen")
    finally:
        conn.close()
