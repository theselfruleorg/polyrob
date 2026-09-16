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


def _chain_id_for_chain(chain: str) -> Optional[int]:
    """The EIP-155 chain id, or ``None`` when this is not an EVM chain.

    ⚠️ ``None`` matters: EIP-681 is an EVM URI, and emitting one for a Solana
    invoice hands a payer a wallet link that resolves to nothing.
    """
    chain = (chain or "").strip().lower()
    if _is_sepolia_chain(chain):
        return _BASE_SEPOLIA_CHAIN_ID
    try:
        from core.wallet import chains as _chains
        row = _chains.get(chain)
    except Exception:
        row = None
    if row is None or row.family != "evm" or not row.chain_id:
        return None
    return int(row.chain_id)


def _invoice_asset(invoice: Dict[str, Any]):
    """``(token_address, decimals, symbol, amount_raw)`` for this invoice.

    046: read from the invoice's OWN asset columns when present. A pre-046
    invoice dict has none, so it resolves the chain's default asset — which is
    exactly what it always meant, so an outstanding invoice's QR never changes
    meaning mid-flight.
    """
    address = (invoice.get("asset_address") or "").strip()
    decimals = invoice.get("asset_decimals")
    symbol = (invoice.get("asset_symbol") or "").strip()
    raw = invoice.get("amount_raw")
    if not address or decimals is None:
        try:
            from modules.x402.invoicing import resolve_invoice_asset
            row = resolve_invoice_asset(str(invoice.get("chain") or "base"), None)
            address = address or (row.address or "")
            decimals = decimals if decimals is not None else row.decimals
            symbol = symbol or row.symbol
        except Exception:
            address = address or ""
            decimals = decimals if decimals is not None else _USDC_DECIMALS
            symbol = symbol or "USDC"
    if raw is None:
        try:
            raw = round(float(invoice.get("amount_usd") or 0)
                        * (10 ** int(decimals)))
        except (TypeError, ValueError):
            raw = 0
    return address, int(decimals), symbol or "USDC", int(raw or 0)


def _format_token_amount(raw: int, decimals: int) -> str:
    """The token amount at FULL precision.

    ⚠️ Never a fixed two decimals. That money format rendered every memecoin
    price as ``0.00`` (the 2026-09-14 finding), and a payer who sends what the
    text shows would send nothing.
    """
    from decimal import Decimal
    value = Decimal(int(raw)) / (Decimal(10) ** int(decimals))
    text = format(value.normalize(), "f")
    return text


def _build_pay_text(*, request_id: str, amount_usd: float, chain: str,
                     recipient: str, purpose: str, expiry_text: str,
                     symbol: str = "USDC", amount_raw: int = 0,
                     decimals: int = 6) -> str:
    """Human pay instructions — same facts as the x402_request result text
    (tools/x402/invoice_tool.py:97-106), reused by the card's "how to pay" block.

    Amount is rendered via `format_invoice_amount` (Task 11 C1 fix) — full
    precision when the amount carries sub-cent jitter, so a payer who pays
    exactly what this text shows can never accidentally settle a DIFFERENT
    same-2dp-amount invoice on-chain."""
    amount_text = format_invoice_amount(amount_usd)
    # 046: name the ACTUAL token. "Pay $0.50 USDC" on a ROB invoice tells the
    # payer to send the wrong asset, which is money they do not get back.
    token_text = (f"{_format_token_amount(amount_raw, decimals)} {symbol}"
                  if symbol and symbol.upper() != "USDC"
                  else f"${amount_text} USDC")
    priced = (f"{token_text} (${amount_text})"
              if symbol and symbol.upper() != "USDC" else token_text)
    lines = [
        f"Pay {priced} on {chain} to {recipient}"
        if recipient else f"Pay {priced} on {chain}",
        f"for: {purpose}" if purpose else "for: (no purpose given)",
        f"request_id: {request_id} · expires {expiry_text}",
    ]
    return "\n".join(lines)


def build_transfer_uri(invoice: Dict[str, Any]) -> Optional[str]:
    """The EIP-681 transfer URI for *invoice*, or ``None``.

    ⚠️ Independent of ``INVOICE_QR_STYLE``. That flag governs what the invoice
    CARD puts in its QR (a bare address scans in more wallets); the API's
    direct-transfer challenge wants the fully-specified URI regardless, because
    its consumer is a payer choosing how to send, not a phone camera.

    ``None`` when the chain is not EVM or the token address is unknown — a
    malformed URI is worse than none.
    """
    recipient = str(invoice.get("recipient") or "").strip()
    if not recipient:
        return None
    token_address, decimals, _symbol, amount_raw = _invoice_asset(invoice)
    chain_id = _chain_id_for_chain(str(invoice.get("chain") or ""))
    if not token_address or not chain_id:
        return None
    return (f"ethereum:{token_address}@{chain_id}/transfer"
            f"?address={recipient}&uint256={amount_raw}")


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

    token_address, decimals, symbol, amount_raw = _invoice_asset(invoice)

    pay_text = _build_pay_text(
        request_id=request_id, amount_usd=amount_usd, chain=chain,
        recipient=recipient, purpose=purpose, expiry_text=expiry_text,
        symbol=symbol, amount_raw=amount_raw, decimals=decimals,
    )

    pay_uri: Optional[str] = None
    if recipient:
        chain_id = _chain_id_for_chain(chain)
        if invoice_qr_style() == "eip681" and token_address and chain_id:
            pay_uri = (f"ethereum:{token_address}@{chain_id}/transfer"
                       f"?address={recipient}&uint256={amount_raw}")
        else:
            # No EVM chain id, or no token address: the bare address is the
            # honest fallback. A malformed EIP-681 URI is worse than none.
            pay_uri = recipient

    return {"pay_text": pay_text, "pay_uri": pay_uri}
