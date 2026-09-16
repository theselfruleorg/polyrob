"""THE payment-asset registry — one row per asset the treasury may be paid in.

Built on the rules `core/wallet/chains.py` already enforces, for the same
reason. Before this module, "the asset" was the literal string ``"usdc"`` in
`modules/x402/invoicing.py`, a ``6`` in `modules/x402/onchain_probe.py`, a pair
of constants in `modules/x402/settlement_scan.py` and a `fastapi_x402` lookup in
`api/x402_endpoints.py`. Adding a second asset meant finding all of them, and
missing one does not error — it silently prices, scans or settles the WRONG
token.

Three rules this table exists to enforce:

* **An asset's values come from its own row.** There is no default row and no
  fallback: an unknown asset resolves to ``None`` and the caller must refuse.
* **Decimals are frozen at registration and never re-read.** They denominate
  money. A token reporting 6 at mint and 18 at settlement would break every
  amount comparison. Same discipline as `core/wallet/tokens.py`.
* **The agent never writes a row.** An asset is an OPERATOR grant, the line
  proposal 036 drew for standing work. The only writer is
  ``polyrob wallet asset``.

Pure core: nothing here imports `agents`/`tools`/`surfaces`/`modules`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)

#: Settlement rails. ``facilitator`` = the fastapi_x402 HTTP 402 path, which is
#: only expressible for an asset that library knows. ``onchain_scan`` = a plain
#: transfer into the treasury, detected by the settlement watcher. Everything an
#: operator pins is ``onchain_scan`` (proposal 046 §8).
#: ``svm_reference`` is a THIRD rail, not a variant of the second: the Solana
#: settlement pass asks the chain which transactions carry an invoice's unique
#: Solana Pay reference, rather than sweeping treasury transfers and matching an
#: amount. Different question, different guarantees — naming it keeps a caller
#: from applying EVM amount-matching reasoning to it.
RAILS = ("facilitator", "onchain_scan", "svm_reference")

#: The Solana USDC mints, mirroring `core/wallet/solana_x402.py::_USDC_MINTS`.
#: Copied rather than imported so this module stays free of the x402 SDK import
#: that file carries; pinned by a test that asserts the two agree.
_SOLANA_USDC_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_SOLANA_USDC_DEVNET = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"

#: ERC-20 view selectors. The same constants `core/wallet/tokens.py` uses.
_SEL_DECIMALS = "0x313ce567"
_SEL_SYMBOL = "0x95d89b41"


@dataclass(frozen=True)
class PaymentAsset:
    """Everything the system knows about one payable asset.

    ``address`` is ``None`` for a chain's native coin. ``symbol`` is
    contract-self-reported and therefore a CLAIM, never a fact — it is for
    display only, and no money math reads it.
    """
    asset_id: str
    chain: str
    address: Optional[str]
    decimals: int
    symbol: str
    rail: str = "onchain_scan"
    #: Hard floor in TOKEN units for any invoice denominated in this asset.
    #: Proposal 046 §4.4: a pumped thin pool makes a USD-priced fee arbitrarily
    #: small in token terms; this is what stops it reaching zero.
    min_amount_raw: int = 0
    #: Below this pool depth the quoter refuses to price against this asset.
    liquidity_floor_usd: float = 0.0
    verified_at: float = 0.0
    #: ``builtin`` or ``operator`` — so a seat can say where a row came from.
    source: str = "builtin"


def _builtins() -> Dict[str, PaymentAsset]:
    """The two rows that reproduce today's behaviour byte-for-byte.

    Addresses come from the constants `core/wallet/onchain.py` already holds and
    `tools/x402/real_client.py` already asset-pins against, so there is no second
    copy to drift.
    """
    from core.wallet.onchain import USDC_BASE_MAINNET, USDC_BASE_SEPOLIA
    rows = (
        PaymentAsset(asset_id="usdc-base", chain="base",
                     address=USDC_BASE_MAINNET, decimals=6, symbol="USDC",
                     rail="facilitator", source="builtin"),
        PaymentAsset(asset_id="usdc-base-sepolia", chain="base-sepolia",
                     address=USDC_BASE_SEPOLIA, decimals=6, symbol="USDC",
                     rail="facilitator", source="builtin"),
        # ⚠️ TWO Solana rows, with DISTINCT ids, because devnet USDC is a
        # different mint and paying the mainnet one on devnet (or the reverse)
        # is a silent misdirection. They share the chain NAME `solana`, so the
        # chain-default lookup must pick between them by the wallet's NETWORK —
        # see `solana_default_asset_id`. An explicit id is never ambiguous.
        PaymentAsset(asset_id="usdc-solana", chain="solana",
                     address=_SOLANA_USDC_MAINNET, decimals=6, symbol="USDC",
                     rail="svm_reference", source="builtin"),
        PaymentAsset(asset_id="usdc-solana-devnet", chain="solana",
                     address=_SOLANA_USDC_DEVNET, decimals=6, symbol="USDC",
                     rail="svm_reference", source="builtin"),
    )
    return {r.asset_id: r for r in rows}


def solana_default_asset_id(network: Optional[str] = None) -> str:
    """Which Solana USDC row a chain-default lookup means, by wallet NETWORK.

    ``mainnet`` -> ``usdc-solana``; anything else (including an unreadable
    wallet) -> ``usdc-solana-devnet``. The testnet default is deliberate: a
    misread that reaches for MAINNET USDC would quote a payer the real-money
    mint on a test run.
    """
    key = (network or "").strip().lower()
    if not key:
        try:
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
            key = str(getattr(wallet, "network", "") or "").strip().lower()
        except Exception as e:
            logger.debug("solana asset default: wallet network unreadable (%s) "
                         "— using devnet", e)
            key = ""
    return "usdc-solana" if key == "mainnet" else "usdc-solana-devnet"


BUILTIN_ASSETS: Dict[str, PaymentAsset] = _builtins()

#: The asset every pre-046 call site implicitly meant.
DEFAULT_ASSET_ID = "usdc-base"

#: ⚠️ Asset ids allowed to name a chain `core/wallet/chains.py` has no row for.
#: A CLOSED set of exactly one. The money chain registry deliberately carries
#: only chains whose addresses were verified on-chain for TRADING, and putting a
#: testnet into ``all_rows()`` would place it in front of every DeFi surface
#: that iterates it. The x402 module already owns the sepolia RPC and chain id
#: (`modules/x402/artifact.py`), so the scan resolves that one itself.
TESTNET_EXEMPT_ASSET_IDS = frozenset({"usdc-base-sepolia"})


def get(asset_id: Optional[str]) -> Optional[PaymentAsset]:
    """The BUILTIN row for *asset_id*, or ``None``. Never another asset."""
    if not asset_id:
        return None
    return BUILTIN_ASSETS.get(str(asset_id).strip().lower())


def known_ids() -> List[str]:
    """Every builtin asset id, so a refusal can echo the vocabulary."""
    return sorted(BUILTIN_ASSETS)


# --------------------------------------------------------------------------
# Operator rows
# --------------------------------------------------------------------------

_DDL = """CREATE TABLE IF NOT EXISTS payment_assets (
    asset_id TEXT PRIMARY KEY,
    chain TEXT NOT NULL,
    address TEXT,
    decimals INTEGER NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    rail TEXT NOT NULL DEFAULT 'onchain_scan',
    min_amount_raw TEXT NOT NULL DEFAULT '0',
    liquidity_floor_usd REAL NOT NULL DEFAULT 0,
    verified_at REAL NOT NULL DEFAULT 0)"""


def store_path(data_home: Optional[str] = None) -> str:
    """``<data_home>/payment_assets.db``.

    Resolved at CALL time, never bound at import — an import-time bind breaks
    ``-P``/profile selection (pinned by tests/test_home_binding_ratchet.py).
    """
    from core.runtime_paths import data_dir_or_home
    return os.path.join(data_dir_or_home(data_home), "payment_assets.db")


class AssetStore:
    """Operator-written asset rows.

    ``min_amount_raw`` is stored as TEXT because an 18-decimal token's raw
    amount exceeds SQLite's signed 64-bit INTEGER range.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        execute_retry(db_path, _DDL)

    def upsert(self, asset: PaymentAsset) -> None:
        execute_retry(
            self.db_path,
            """INSERT INTO payment_assets(asset_id,chain,address,decimals,symbol,
                   rail,min_amount_raw,liquidity_floor_usd,verified_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(asset_id) DO UPDATE SET
                   chain=excluded.chain, address=excluded.address,
                   decimals=excluded.decimals, symbol=excluded.symbol,
                   rail=excluded.rail, min_amount_raw=excluded.min_amount_raw,
                   liquidity_floor_usd=excluded.liquidity_floor_usd,
                   verified_at=excluded.verified_at""",
            (asset.asset_id.strip().lower(), asset.chain.strip().lower(),
             (asset.address or "").strip().lower() or None,
             int(asset.decimals), asset.symbol, asset.rail,
             str(int(asset.min_amount_raw)), float(asset.liquidity_floor_usd),
             float(asset.verified_at)))

    def _row_to_asset(self, r) -> PaymentAsset:
        return PaymentAsset(
            asset_id=r["asset_id"], chain=r["chain"], address=r["address"],
            decimals=int(r["decimals"]), symbol=r["symbol"], rail=r["rail"],
            min_amount_raw=int(r["min_amount_raw"]),
            liquidity_floor_usd=float(r["liquidity_floor_usd"]),
            verified_at=float(r["verified_at"]), source="operator")

    def get(self, asset_id: Optional[str]) -> Optional[PaymentAsset]:
        if not asset_id:
            return None
        row = execute_retry(
            self.db_path, "SELECT * FROM payment_assets WHERE asset_id = ?",
            (str(asset_id).strip().lower(),), fetch="one")
        return self._row_to_asset(row) if row else None

    def list_all(self) -> List[PaymentAsset]:
        rows = execute_retry(
            self.db_path, "SELECT * FROM payment_assets ORDER BY asset_id",
            fetch="all") or []
        return [self._row_to_asset(r) for r in rows]


def _store(data_home: Optional[str] = None) -> Optional[AssetStore]:
    """An open store, or ``None``.

    Fail-open: an unreadable store costs the OPERATOR rows, never the builtin
    ones — so a corrupt sidecar degrades to "USDC only", which is the pre-046
    behaviour, not an outage.
    """
    try:
        return AssetStore(store_path(data_home))
    except Exception as e:
        logger.warning("payment asset store unreadable (%s) — builtins only", e)
        return None


def resolve(asset_id: Optional[str], *,
            data_home: Optional[str] = None) -> Optional[PaymentAsset]:
    """The row for *asset_id*.

    An operator row wins over a builtin of the same id, so an operator can
    tighten ``min_amount_raw`` on USDC without a code change. ``None`` for an
    unknown id — never another asset.
    """
    if not asset_id:
        return None
    store = _store(data_home)
    if store is not None:
        try:
            row = store.get(asset_id)
            if row is not None:
                return row
        except Exception as e:
            logger.warning("payment asset lookup failed for %r (%s) — builtins "
                           "only", asset_id, e)
    return get(asset_id)


def all_assets(*, data_home: Optional[str] = None) -> List[PaymentAsset]:
    """Every resolvable asset, operator rows shadowing builtins by id."""
    merged = dict(BUILTIN_ASSETS)
    store = _store(data_home)
    if store is not None:
        try:
            for row in store.list_all():
                merged[row.asset_id] = row
        except Exception as e:
            logger.warning("payment asset listing failed (%s) — builtins only", e)
    return [merged[k] for k in sorted(merged)]


def vocabulary(*, data_home: Optional[str] = None) -> str:
    """Comma-joined asset ids, for a refusal that echoes what IS known."""
    return ", ".join(a.asset_id for a in all_assets(data_home=data_home))


# --------------------------------------------------------------------------
# One-time on-chain freeze
# --------------------------------------------------------------------------

def _decode_abi_string(hexdata: str) -> str:
    """Decode a solidity ``string`` return.

    Falls back to a bytes32-style read for the older tokens that return one
    (MKR and friends), which is why this is not a one-liner.
    """
    raw = bytes.fromhex((hexdata or "0x").removeprefix("0x"))
    if len(raw) >= 64:
        offset = int.from_bytes(raw[0:32], "big")
        if offset == 32:
            length = int.from_bytes(raw[32:64], "big")
            return raw[64:64 + length].decode("utf-8", "replace").strip("\x00")
    return raw.decode("utf-8", "replace").strip("\x00")


def verify_on_chain(chain: str, address: str, *, rpc_call) -> Tuple[int, str]:
    """``(decimals, symbol)`` read from the contract ONCE, for freezing.

    ⚠️ ``symbol()`` and ``decimals()`` are chosen by the token contract and are
    therefore attacker-authored (`core/wallet/tokens.py`'s trust note). Reading
    them does not make them true. This function exists to capture them at a
    moment an OPERATOR chose, so every later money comparison reads a value that
    cannot move under us.

    Raises ``ValueError`` when the address carries no code — an asset row for a
    bare EOA would scan a contract that emits no Transfer logs, and every
    invoice against it would sit pending forever.
    """
    code = rpc_call("eth_getCode", [address, "latest"])
    if not code or str(code) in ("0x", "0x0"):
        raise ValueError(f"{address} has no code on {chain} — not a token contract")
    dec_raw = rpc_call("eth_call", [{"to": address, "data": _SEL_DECIMALS}, "latest"])
    try:
        decimals = int(dec_raw, 16)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{address} did not answer decimals() on {chain}") from e
    if not 0 <= decimals <= 36:
        raise ValueError(f"{address} reports {decimals} decimals, which is not usable")
    sym_raw = rpc_call("eth_call", [{"to": address, "data": _SEL_SYMBOL}, "latest"])
    try:
        symbol = _decode_abi_string(sym_raw)[:32]
    except Exception:
        symbol = ""
    return decimals, symbol


__all__ = ["AssetStore", "BUILTIN_ASSETS", "DEFAULT_ASSET_ID", "PaymentAsset",
           "RAILS", "TESTNET_EXEMPT_ASSET_IDS", "all_assets", "get", "known_ids",
           "resolve", "solana_default_asset_id", "store_path", "verify_on_chain",
           "vocabulary"]
