"""Asset resolution and asset-keyed settlement matching for x402 invoices.

Extracted from `invoicing.py` (046 Phase 0). The repo's standing decomposition
rule is that new behaviour gets its own module rather than growing a god file,
and `invoicing.py` sits under a size ratchet for exactly that reason.

Two concerns live here, and they are one concern seen from both ends:

* **Minting** — WHICH token is this invoice payable in, at what precision, in
  what raw amount.
* **Settling** — which pending invoice does an observed on-chain transfer of a
  given token, in a given raw amount, belong to.

⚠️ The asset must be part of BOTH. Before 046 the match compared a float
``amount_usd`` treasury-wide with no asset filter, which was safe only because
exactly one contract was ever queried.

`invoicing` re-exports every public name here, so no caller had to change.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from modules.x402._db import resolve_db as _resolve_db

logger = logging.getLogger(__name__)

#: The metadata kind an AGENT-created invoice carries. Defined here and imported
#: BY `invoicing` (not the reverse): `invoicing` imports this module for the
#: re-exports, so a module-level import back would close the cycle.
INVOICE_KIND = "agent_invoice"


def _payable_kinds() -> tuple:
    """Every metadata kind the settlement matcher will settle.

    Read lazily from `invoicing.PAYABLE_KINDS` (which imports THIS module, so a
    module-level import back would close the cycle). The jitter needs it because
    the matcher is kind-blind: uniqueness asserted per-producer left an agent
    invoice and a room offer at the same price colliding.
    """
    try:
        from modules.x402.invoicing import PAYABLE_KINDS
        return tuple(PAYABLE_KINDS)
    except Exception:
        return (INVOICE_KIND,)


def _row_metadata(row: Dict[str, Any]) -> dict:
    from modules.x402.invoicing import _row_metadata as _impl
    return _impl(row)


def normalize_recipient(address: str, chain: Optional[str] = None) -> str:
    from modules.x402.invoicing import normalize_recipient as _impl
    return _impl(address, chain)


def _chain_family(chain: str) -> str:
    from modules.x402.invoicing import _chain_family as _impl
    return _impl(chain)

def resolve_invoice_asset(chain: Optional[str], asset_id: Optional[str]):
    """The `PaymentAsset` this invoice is payable in (046 Phase 0).

    Precedence: the explicit id, else ``PAYMENT_DEFAULT_ASSET``, else the
    chain's own canonical USDC — which is what every pre-046 caller meant.

    Raises ``ValueError`` naming the vocabulary rather than falling back to
    another asset. An invoice quoted in the wrong token is money sent to a
    contract nobody is watching, and an asset on the wrong CHAIN is money
    stranded permanently.
    """
    from core.payments import assets as _assets
    chain = (chain or "").strip().lower()
    requested = (asset_id or os.getenv("PAYMENT_DEFAULT_ASSET", "").strip()
                 or "").strip().lower()
    if not requested:
        # ⚠️ Solana carries TWO USDC rows (mainnet and devnet are different
        # mints), so "the chain's USDC" is ambiguous there and the WALLET's
        # network decides. Everywhere else one row per chain is unambiguous.
        if _chain_family(chain) == "svm":
            requested = _assets.solana_default_asset_id()
        else:
            for row in _assets.all_assets():
                if row.chain == chain and (row.symbol or "").upper() == "USDC":
                    return row
            raise ValueError(
                f"no default payable asset for chain {chain!r} — pin one with "
                f"`polyrob wallet asset add --chain {chain} ...`, or pass "
                f"asset_id (known: {_assets.vocabulary()})")
    row = _assets.resolve(requested)
    if row is None:
        raise ValueError(
            f"unknown payment asset {requested!r} (known: {_assets.vocabulary()})")
    if row.chain != chain:
        raise ValueError(
            f"asset {row.asset_id!r} lives on {row.chain!r}, not on {chain!r} — "
            f"paying it on the wrong chain strands the funds permanently")
    return row


def atomic_amount(amount_usd: float, decimals: int) -> int:
    """A USD figure to raw units at *decimals*, via Decimal so a float cannot
    round the last unit away.

    ⚠️ Only correct when the asset is dollar-pegged. A non-stable asset MUST
    pass ``amount_raw`` explicitly — sizing a volatile token is the quoter's
    job (`core/payments/quote.py`), not this module's.
    """
    from decimal import Decimal
    return int((Decimal(str(amount_usd)) * (10 ** int(decimals))).to_integral_value())


async def match_pending_invoice(
    treasury: str, asset_address: Optional[str], amount_raw: int, *,
    chain: Optional[str] = None, decimals: Optional[int] = None,
    kinds: tuple = (INVOICE_KIND,), db=None,
) -> Optional[Dict[str, Any]]:
    """The on-chain settlement-detection ambiguity policy, ASSET-KEYED.

    Among PENDING invoices for this treasury IN THIS ASSET at an EXACT
    ``amount_raw`` match, the OLDEST wins (``created_at`` ascending, the table's
    implicit ``rowid`` to break a same-second tie — NOT ``id``, which is a
    random UUID and carries no chronological meaning), so one detected transfer
    settles at most one invoice.

    ⚠️ The asset is part of the key. Before 046 this matched a FLOAT
    ``amount_usd`` treasury-wide, which was safe only while exactly one contract
    was ever scanned: with two, a $1 transfer of a worthless token settles a $1
    USDC invoice. Matching an INTEGER also survives 18 decimals, which the float
    did not.

    ⚠️ Legacy rows (minted before the asset columns existed) carry NULL
    ``asset_address`` and NULL ``amount_raw``. They are matched on the OLD terms
    — ``amount_usd`` derived from ``amount_raw``/``decimals`` — so an invoice
    outstanding across the deploy is never orphaned. That fallback REQUIRES
    ``decimals``; without it a legacy row is skipped rather than matched wrongly.

    ⚠️ ``kinds`` is the producer filter. A kind outside the set is invisible
    here, so a new invoice producer that forgets to widen it mints rows that sit
    pending forever with the money already received.

    Not tenant-scoped: an on-chain transfer carries no tenant identity, only the
    recipient (treasury) address. Disambiguating same-amount invoices is what
    the amount jitter is for.
    """
    if not treasury or not amount_raw:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    recipient = normalize_recipient(treasury, chain)
    placeholders = ",".join("?" for _ in kinds)
    row = await database.fetch_one(
        f"""SELECT * FROM x402_payment_requests
            WHERE status = 'pending' AND recipient = ?
              AND amount_raw = ?
              AND lower(COALESCE(asset_address, '')) = ?
              AND json_extract(metadata, '$.kind') IN ({placeholders})
            ORDER BY created_at ASC, rowid ASC LIMIT 1""",
        (recipient, str(int(amount_raw)),
         (asset_address or "").strip().lower(), *kinds),
    )
    if row is None and decimals is not None:
        legacy_usd = round(int(amount_raw) / (10 ** int(decimals)), 6)
        row = await database.fetch_one(
            f"""SELECT * FROM x402_payment_requests
                WHERE status = 'pending' AND recipient = ?
                  AND amount_raw IS NULL AND asset_address IS NULL
                  AND amount_usd = ?
                  AND json_extract(metadata, '$.kind') IN ({placeholders})
                ORDER BY created_at ASC, rowid ASC LIMIT 1""",
            (recipient, legacy_usd, *kinds),
        )
    if not row:
        return None
    meta = _row_metadata(row)
    return {
        "request_id": row["id"],
        "amount_usd": row.get("amount_usd"),
        "amount_raw": row.get("amount_raw"),
        "asset_id": row.get("asset_id") or "usdc-base",
        "kind": meta.get("kind") or INVOICE_KIND,
        "room_action": meta.get("room_action"),
        "session_id": meta.get("session_id") or "",
        "user_id": meta.get("tenant_id") or row.get("user_id") or "",
    }


async def match_pending_invoice_by_amount(
    amount_usd: float, treasury: str, *, chain: Optional[str] = None, db=None,
) -> Optional[Dict[str, Any]]:
    """Back-compat shim for the pre-046 USDC-only call shape.

    Resolves ``usdc-base`` and delegates to :func:`match_pending_invoice`. Kept
    so no caller breaks; new code passes the asset explicitly.
    """
    from core.payments import assets as _assets
    row = _assets.resolve(_assets.DEFAULT_ASSET_ID)
    decimals = row.decimals if row else 6
    raw = round(float(amount_usd) * (10 ** decimals))
    return await match_pending_invoice(
        treasury, row.address if row else None, raw, chain=chain,
        decimals=decimals, db=db)
