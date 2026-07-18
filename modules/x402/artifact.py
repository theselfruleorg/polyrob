"""Payment-artifact helper — the facts an invoice card/QR needs (Task 6, Phase 1).

``build_payment_artifact(invoice)`` turns an invoice dict (the shape returned by
``modules.x402.invoicing.create_payment_request`` / ``get_payment_request``) into
``{"pay_text": str, "pay_uri": str | None}``:

- ``pay_text`` carries the same facts already shown in the ``x402_request`` action
  result (``tools/x402/invoice_tool.py``): request_id, amount (USDC), chain,
  recipient, purpose, expiry — reused verbatim by the invoice card's "how to pay"
  block so the text-only and card surfaces never disagree.
- ``pay_uri`` is what the card's QR code encodes, chosen by ``INVOICE_QR_STYLE``:
  - ``address`` (default) — the bare treasury address string. Works with any
    wallet's "scan an address" import.
  - ``eip681`` — an EIP-681 USDC transfer URI
    (``ethereum:<usdc_contract>@<chain_id>/transfer?address=<treasury>&uint256=<atomic>``)
    that pre-fills the amount for wallets that support it. The USDC contract
    address constants are the SAME ones ``core/wallet/onchain.py`` already trusts
    for on-chain balance reads (no second copy to drift).
  ``pay_uri`` is ``None`` when the invoice has no recipient — the card renderer
  omits the QR block cleanly in that case.

Pure/stateless: no I/O, no network, no randomness. Never raises on a well-formed
invoice dict; unexpected/missing fields degrade to empty-string facts rather than
raising, so a render failure downstream is a card-only concern (fail-open, Task 6).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.wallet.onchain import USDC_BASE_MAINNET, USDC_BASE_SEPOLIA

# Base mainnet / Base Sepolia testnet chain ids (CAIP-2 numeric suffix), mirroring
# the mainnet/testnet split tools/x402/real_client.py already uses.
_BASE_CHAIN_ID = 8453
_BASE_SEPOLIA_CHAIN_ID = 84532
_USDC_DECIMALS = 6
_SEPOLIA_CHAIN_NAMES = frozenset({"base-sepolia", "base_sepolia", "basesepolia"})

_QR_STYLES = frozenset({"address", "eip681"})
_DEFAULT_QR_STYLE = "address"


def invoice_qr_style() -> str:
    """Resolve ``INVOICE_QR_STYLE`` (default ``address``); an unrecognized value
    degrades to the default rather than raising or emitting a broken QR payload.

    Task 11 C1 fix: when the caller leaves ``INVOICE_QR_STYLE`` UNSET AND on-chain
    settlement detection (``X402_SETTLE_ONCHAIN_DETECT``) is active, this prefers
    ``eip681`` over the bare-address default — the eip681 URI encodes the EXACT
    atomic USDC amount, which is unambiguous regardless of how the payer-facing
    text renders. An operator's EXPLICIT ``INVOICE_QR_STYLE`` always wins; only
    the un-set default is upgraded."""
    raw = os.getenv("INVOICE_QR_STYLE")
    style = (raw or _DEFAULT_QR_STYLE).strip().lower()
    if style not in _QR_STYLES:
        style = _DEFAULT_QR_STYLE
    if raw is None or not raw.strip():
        try:
            from modules.x402.invoicing import x402_settle_onchain_detect_enabled
            if x402_settle_onchain_detect_enabled():
                return "eip681"
        except Exception:
            pass
    return style


def format_invoice_amount(amount_usd: Any) -> str:
    """Render a USD amount for PAYER-FACING text (Task 11 C1 fix).

    ``_dedupe_amount_for_treasury`` (``modules/x402/invoicing.py``) nudges the
    STORED amount by deterministic sub-cent ($0.0001) steps to keep on-chain
    amount-matching unambiguous — but if the payer-facing text always rounds to
    2dp, a payer paying the DISPLAYED "$7.50" for a jittered $7.5001 invoice
    exact-matches an OLDER, unrelated $7.50 invoice on-chain (oldest-first
    ambiguity policy) — a cross-tenant misdirected settlement. This renders the
    FULL precision whenever sub-cent digits are present (never truncates below
    2dp): ``7.50`` stays ``7.50``; ``7.5001`` renders as ``7.5001``.

    Rounds to 6dp first (USDC's own on-chain precision) to kill float noise,
    matching the rounding `_dedupe_amount_for_treasury` and the DB column
    already use, so this can never disagree with what the watcher matches
    against on-chain.
    """
    try:
        amount = float(amount_usd or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    rounded6 = round(amount, 6)
    two_dp = round(rounded6, 2)
    if abs(rounded6 - two_dp) < 1e-9:
        return f"{two_dp:.2f}"
    text = f"{rounded6:.6f}".rstrip("0")
    if text.endswith("."):
        text += "00"
    return text


def _format_expiry(epoch: Any) -> str:
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "unknown"


def _is_sepolia_chain(chain: str) -> bool:
    return chain.strip().lower() in _SEPOLIA_CHAIN_NAMES


def _usdc_contract_for_chain(chain: str) -> str:
    return USDC_BASE_SEPOLIA if _is_sepolia_chain(chain) else USDC_BASE_MAINNET


def _chain_id_for_chain(chain: str) -> int:
    return _BASE_SEPOLIA_CHAIN_ID if _is_sepolia_chain(chain) else _BASE_CHAIN_ID


def _atomic_usdc_amount(amount_usd: Any) -> int:
    try:
        return round(float(amount_usd or 0) * (10 ** _USDC_DECIMALS))
    except (TypeError, ValueError):
        return 0


def _build_pay_text(*, request_id: str, amount_usd: float, chain: str,
                     recipient: str, purpose: str, expiry_text: str) -> str:
    """Human pay instructions — same facts as the x402_request result text
    (tools/x402/invoice_tool.py:97-106), reused by the card's "how to pay" block.

    Amount is rendered via `format_invoice_amount` (Task 11 C1 fix) — full
    precision when the amount carries sub-cent jitter, so a payer who pays
    exactly what this text shows can never accidentally settle a DIFFERENT
    same-2dp-amount invoice on-chain."""
    amount_text = format_invoice_amount(amount_usd)
    lines = [
        f"Pay ${amount_text} USDC on {chain} to {recipient}"
        if recipient else f"Pay ${amount_text} USDC on {chain}",
        f"for: {purpose}" if purpose else "for: (no purpose given)",
        f"request_id: {request_id} · expires {expiry_text}",
    ]
    return "\n".join(lines)


def build_payment_artifact(invoice: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Return ``{"pay_text": str, "pay_uri": str | None}`` for ``invoice``.

    ``invoice`` is the dict shape ``create_payment_request``/``get_payment_request``
    return (request_id/amount_usd/chain/recipient/purpose/expires_at_epoch). Missing
    fields degrade gracefully rather than raising.
    """
    request_id = str(invoice.get("request_id") or "")
    try:
        amount_usd = float(invoice.get("amount_usd") or 0.0)
    except (TypeError, ValueError):
        amount_usd = 0.0
    chain = str(invoice.get("chain") or "base")
    recipient = str(invoice.get("recipient") or "").strip()
    purpose = str(invoice.get("purpose") or "").strip()
    expiry_text = _format_expiry(invoice.get("expires_at_epoch"))

    pay_text = _build_pay_text(
        request_id=request_id, amount_usd=amount_usd, chain=chain,
        recipient=recipient, purpose=purpose, expiry_text=expiry_text,
    )

    pay_uri: Optional[str] = None
    if recipient:
        if invoice_qr_style() == "eip681":
            contract = _usdc_contract_for_chain(chain)
            chain_id = _chain_id_for_chain(chain)
            atomic = _atomic_usdc_amount(amount_usd)
            pay_uri = f"ethereum:{contract}@{chain_id}/transfer?address={recipient}&uint256={atomic}"
        else:
            pay_uri = recipient

    return {"pay_text": pay_text, "pay_uri": pay_uri}
