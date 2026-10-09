"""An independent value floor for a bridge quote, before signing."""
from decimal import Decimal


def quote_refusal(tool, quote, *, origin: str, destination: str) -> str | None:
    from core.wallet import chains
    from core.wallet.tokens import get_token_identity, bounded_decimals
    from tools.defi.trade_tool import _route_drift_max_pct
    from tools.defi.providers.relay_bridge import NATIVE_EVM

    try:
        source, target = chains.get(origin), chains.get(destination)
        if not source or not target or not source.wrapped_native:
            return "independent bridge valuation unavailable: unknown native asset"
        currency = str(quote.currency_out).lower()
        if currency in {NATIVE_EVM.lower(), str(target.wrapped_native).lower()}:
            token_out, decimals_out = target.wrapped_native, target.native_decimals
        elif target.usdc and currency == target.usdc.lower():
            token_out = target.usdc
            decimals_out = get_token_identity(destination, token_out).decimals
        else:
            return "independent bridge valuation unavailable: unpinned destination asset"
        if (bounded_decimals(decimals_out) is None
                or quote.decimals_out != decimals_out):
            return "bridge quote decimals disagree with independent destination metadata"
        raw_prices = (tool._price(origin, source.wrapped_native),
                      tool._price(destination, token_out))
        prices = [Decimal(str(value)) for value in raw_prices
                  if not isinstance(value, bool)]
        if len(prices) != 2 or any(not p.is_finite() or p <= 0 for p in prices):
            return "independent bridge valuation unavailable: both assets need spend-grade prices"
        limit = Decimal(str(_route_drift_max_pct()))
        impact = Decimal(str(quote.impact_pct))
        if not impact.is_finite() or abs(impact) > limit:
            return "bridge provider impact exceeds the route drift limit or is unknown"
        value_in = Decimal(quote.amount_in_raw) * prices[0] / 10 ** source.native_decimals
        value_floor = Decimal(quote.min_out_raw) * prices[1] / 10 ** decimals_out
        required = value_in * (1 - limit / 100)
        if value_in <= 0 or value_floor < required:
            return (f"bridge arrival floor is worth ${value_floor:.4f}; at least "
                    f"${required:.4f} is required by independent prices ({limit}% maximum loss)")
    except Exception:
        return "independent bridge valuation unavailable; no live bridge may proceed"
    return None
