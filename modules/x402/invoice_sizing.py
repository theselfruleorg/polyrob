"""Sizing a USD invoice into RAW token units — the 046 §4.4 join.

`core/payments/quote.py` holds the POLICY (freshness, screen verdict,
liquidity floor, token floor) and `tools/defi/payment_quote.py` holds the
READ. Neither was reachable from the mint path: `create_payment_request`
fell through to ``atomic_amount`` — PAR math — whenever the caller passed no
``amount_raw``, and the only agent-facing caller (`x402_request`) never
passes one. A $5 invoice in a token worth $0.00006 therefore asked for 5
tokens instead of ~78,567: wrong by four orders of magnitude, and silent.

Its own module because `invoicing.py` is under a size ratchet, for the same
reason `invoice_jitter.py` is a separate file.
"""
from __future__ import annotations

import logging
from typing import Any

from modules.x402.invoice_assets import atomic_amount

logger = logging.getLogger(__name__)

#: ``quoter`` sentinel. ⚠️ An explicit ``None`` means "there is NO quoter" and
#: must refuse a non-stable asset; omitting the argument means "resolve one from
#: the container". Collapsing the two would turn a test's deliberate no-quoter
#: case, and a caller that genuinely has none, into a silent container lookup.
_UNSET = object()


def _resolve_quoter(quoter: Any):
    """The ``payment_quoter`` container service, or None. Fail-open by design:
    a missing quoter is a REFUSAL at sizing time (`size_amount_raw`), never a
    fallback price."""
    if quoter is not _UNSET:
        return quoter
    try:
        from core.container import DependencyContainer
        return DependencyContainer.get_instance().get_service("payment_quoter")
    except Exception:
        return None


def _size_invoice_raw(amount_usd: float, asset, amount_raw, quoter: Any) -> int:
    """Raw units for this invoice.

    ⚠️ An explicit ``amount_raw`` wins untouched. A dollar-pegged asset keeps
    the legacy ``atomic_amount`` path byte-for-byte. Everything else is sized by
    the 046 §4.4 quoter policy — because ``atomic_amount`` is par math, and
    applying it to a token worth $0.00006 mints an invoice four orders of
    magnitude too small, silently.
    """
    if amount_raw is not None:
        return int(amount_raw)
    from core.payments.quote import QuoteRefused, is_stable, size_amount_raw
    if is_stable(asset):
        return atomic_amount(amount_usd, asset.decimals)
    quote = None
    resolved = _resolve_quoter(quoter)
    if resolved is not None:
        try:
            quote = resolved.quote(asset)
        except Exception as e:
            logger.warning("x402 invoice: quoter raised for %s (%s) — no quote",
                           asset.asset_id, e)
    try:
        return size_amount_raw(amount_usd, asset, quote)
    except QuoteRefused as e:
        raise ValueError(str(e)) from e


def scale_raw(base_raw: int, base_usd: float, usd: float) -> int:
    """Scale the ONE sizing decision to a (possibly jittered) USD figure.

    ⚠️ Never re-quotes. A second quote can return a different price, and the
    row would then name a payable integer nobody was ever quoted — which the
    exact-amount settlement match would never fire on. Decimal throughout, so a
    float cannot round the last raw unit away.
    """
    if base_usd <= 0:
        return base_raw
    from decimal import Decimal
    scaled = Decimal(base_raw) * Decimal(str(usd)) / Decimal(str(base_usd))
    return max(1, int(scaled))


__all__ = ["_UNSET", "_resolve_quoter", "_size_invoice_raw", "scale_raw"]
