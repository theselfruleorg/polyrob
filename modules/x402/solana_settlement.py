"""SPL settlement detection and Solana Pay `reference` matching. Phase 4.

The EVM settlement watcher matches an incoming payment to a pending invoice by
EXACT AMOUNT, and needs `X402_INVOICE_AMOUNT_JITTER` so two invoices priced the
same stay distinguishable. That jitter is a workaround for a missing correlator,
not a feature — it is disclosed to payers precisely because it is a wart.

**Solana has the correlator.** Solana Pay puts a `reference` public key into the
transaction's account list: unique per invoice, carried by the payer, and
directly queryable with `getSignaturesForAddress` on the reference itself. So
matching is exact and deterministic, two invoices of the same amount are never
ambiguous, and amount-jitter here would be strictly worse (Solana research
mismatch #8). This module therefore does NOT jitter.

The reference is derived from the invoice id alone — deliberately with **no
secret**. It is a MARKER, not an account: nobody should ever be able to sign for
it, and deriving it from a key would make it spendable.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58_ALPHABET[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + (out or "1")


def reference_for_invoice(invoice_id: str) -> str:
    """The Solana Pay reference key for *invoice_id*.

    Deterministic, so the watcher can re-derive it without storing anything, and
    derived from the invoice id ALONE, so it is a marker rather than a key
    anyone could spend from. A 32-byte digest is a valid ed25519-shaped account
    key whether or not it lies on the curve — Solana Pay references routinely do
    not, which is fine because nothing ever signs for one.
    """
    digest = hashlib.sha256(f"polyrob-x402-invoice:{invoice_id}".encode()).digest()
    return _b58encode(digest)


def match_by_reference(known_references: Iterable[str],
                       observed_reference: str) -> Optional[str]:
    """The observed reference if we issued it, else ``None``.

    Exact, not fuzzy. Where the EVM watcher must reason about amounts, this
    simply asks whether the marker is one of ours.
    """
    if not observed_reference:
        return None
    for ref in known_references:
        if ref == observed_reference:
            return ref
    return None


def credited_amount(tx: Any, *, treasury: str, mint: str) -> Optional[int]:
    """Raw token units credited to *treasury* in *mint* by this transaction.

    ``None`` for anything that is not a clean incoming credit — a reverted
    transaction, another mint, another owner, or a DECREASE. Computed from the
    pre/post token balances the RPC already returns, so no separate balance read
    can race it.

    A landed-but-failed transaction credits nothing: the fee was paid, the
    payment was not, and treating it as settlement would mark an invoice paid
    for a transfer that never happened.
    """
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        return None
    if meta.get("err") is not None:
        return None

    def _sum(entries: Any) -> Optional[int]:
        if not isinstance(entries, list):
            return None
        total = 0
        seen = False
        for e in entries:
            if not isinstance(e, dict):
                continue
            if e.get("owner") != treasury or e.get("mint") != mint:
                continue
            try:
                total += int(e["uiTokenAmount"]["amount"])
                seen = True
            except (KeyError, TypeError, ValueError):
                continue
        return total if seen else None

    post = _sum(meta.get("postTokenBalances"))
    if post is None:
        return None
    pre = _sum(meta.get("preTokenBalances")) or 0
    delta = post - pre
    return delta if delta > 0 else None


def settlement_for(tx: Any, *, treasury: str, mint: str,
                   expected_raw: int) -> Optional[int]:
    """The amount to settle an invoice for, or ``None`` to leave it pending.

    The REFERENCE already identified which invoice this is; the amount only
    VALIDATES it. That is the whole difference from the EVM watcher, where the
    amount does the identifying and therefore needs jitter to stay unambiguous.

    An OVERPAYMENT settles (a payer who rounds up must not strand their own
    invoice); an underpayment does not, because a partially-paid invoice is not
    a paid one and silently accepting it would understate what is owed.
    """
    credited = credited_amount(tx, treasury=treasury, mint=mint)
    if credited is None:
        return None
    return credited if credited >= int(expected_raw) else None


def scan_reference(reference: str, *, rpc, limit: int = 20) -> Iterable[str]:
    """Signatures that touched *reference*, newest first.

    This is the whole reason the reference exists: instead of scanning every
    transfer to the treasury and guessing which invoice it belongs to, ask the
    chain directly which transactions carry THIS invoice's marker.
    """
    try:
        res = rpc("getSignaturesForAddress", [reference, {"limit": int(limit)}])
    except Exception as exc:
        logger.info("solana settlement: reference scan failed for %s (%s)",
                    reference, exc)
        return []
    out = []
    for entry in (res or []):
        if isinstance(entry, dict) and entry.get("signature"):
            if entry.get("err") is None:
                out.append(str(entry["signature"]))
    return out
