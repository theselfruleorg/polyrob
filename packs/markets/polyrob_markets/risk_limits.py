"""Fail-closed venue limits over complete account and live market snapshots."""
import math


class RiskLimitError(ValueError):
    """A locally authored refusal safe to show to the caller."""


def nonnegative(value):
    if isinstance(value, bool):
        raise RiskLimitError("Invalid numeric risk data")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise RiskLimitError("Invalid numeric risk data")
    return value


def validate_limits(limits):
    for name, value in vars(limits).items():
        if name.startswith(("max_", "min_", "require_confirmation_")):
            nonnegative(value)


def check_book(limits, result):
    """Measure displayed depth inside the allowed spread around the best quotes."""
    if result.get("success") is not True:
        raise RiskLimitError("Cannot verify orderbook limits")
    bids, asks = result["bids"], result["asks"]
    if not bids or not asks:
        raise RiskLimitError("Cannot verify two-sided orderbook")
    bid_rows = [(nonnegative(r["price"]), nonnegative(r["size"])) for r in bids]
    ask_rows = [(nonnegative(r["price"]), nonnegative(r["size"])) for r in asks]
    best_bid, best_ask = max(p for p, _ in bid_rows), min(p for p, _ in ask_rows)
    tolerance = nonnegative(limits.max_spread_tolerance)
    if best_bid <= 0 or best_ask < best_bid or (best_ask - best_bid) / best_bid > tolerance:
        raise RiskLimitError("Orderbook spread exceeds configured limit")
    # Far-away resting quotes are not executable liquidity at the accepted prices.
    liquidity = sum(p * s for p, s in bid_rows if p >= best_bid * (1 - tolerance))
    liquidity += sum(p * s for p, s in ask_rows if p <= best_ask * (1 + tolerance))
    if nonnegative(liquidity) < nonnegative(limits.min_liquidity_required):
        raise RiskLimitError("Orderbook liquidity is below configured minimum")


def check_exposure(limits, positions, orders, market_id, new_value):
    """Count current positions AND unfilled opening orders, without netting sides."""
    total = per_market = 0.0
    for row in positions:
        value = nonnegative(row["value"])
        if not row.get("market_id"):
            raise RiskLimitError("Cannot identify position market")
        total += value
        if row["market_id"] == market_id:
            per_market += value
    for row in orders:
        if row.get("reduce_only") is True:
            continue
        if not row.get("market_id"):
            raise RiskLimitError("Cannot identify open order market")
        size, filled = nonnegative(row["size"]), nonnegative(row.get("size_matched", 0))
        if filled > size:
            raise RiskLimitError("Invalid open order quantities")
        value = (size - filled) * nonnegative(row["price"])
        total += value
        if row["market_id"] == market_id:
            per_market += value
    amount = nonnegative(new_value)
    if nonnegative(per_market + amount) > nonnegative(limits.max_position_per_market_usd):
        raise RiskLimitError("Per-market position cap would be exceeded (including open orders)")
    if nonnegative(total + amount) > nonnegative(limits.max_total_exposure_usd):
        raise RiskLimitError("Total exposure cap would be exceeded (including open orders)")


async def polymarket_limits(tool, limits, params, notional):
    # Imported at call time because parameter models live alongside the service.
    from polyrob_markets.polymarket.service import (
        MarketDetailsParams, GetOrderbookParams, GetPositionsParams, GetOpenOrdersParams,
    )
    validate_limits(limits)
    details = await tool.get_market_details(MarketDetailsParams(market_id=params.market_id))
    if details.get("success") is not True:
        raise RiskLimitError("Cannot verify market identity and restrictions")
    market = details["market"]
    condition = market["condition_id"]
    if not condition or params.token_id not in {r["token_id"] for r in market["outcomes"]}:
        raise RiskLimitError("Token does not belong to the verified market")
    identities = {str(v).casefold() for v in (condition, market.get("slug"), params.market_id) if v}
    if identities.intersection(str(v).casefold() for v in limits.blocked_markets):
        raise RiskLimitError("Market is blocked by trading limits")
    categories = {v.casefold() for v in limits.allowed_categories}
    if "*" not in categories and str(market.get("category", "")).casefold() not in categories:
        raise RiskLimitError("Market category is not allowed by trading limits")
    if market.get("active") is not True or market.get("closed") is not False:
        raise RiskLimitError("Market is not open for trading")
    check_book(limits, await tool.get_orderbook(GetOrderbookParams(token_id=params.token_id, depth=100)))
    if params.side.upper() == "SELL":
        return None  # Polymarket cannot short; only the exposure check is exempt.
    positions = await tool.get_all_positions(GetPositionsParams(include_closed=False))
    orders = await tool.get_open_orders(GetOpenOrdersParams())
    if positions.get("success") is not True or orders.get("success") is not True:
        raise RiskLimitError("Cannot verify positions and open orders")
    opening = []
    for row in orders["orders"]:
        if row["side"].upper() not in {"BUY", "SELL"}:
            raise RiskLimitError("Cannot determine open order side")
        if row["side"].upper() == "BUY":
            opening.append(row)
    check_exposure(limits, positions["positions"], opening, condition, notional)
    return None


async def hyperliquid_limits(tool, credentials, coin, notional, reduce_only):
    from polyrob_markets.hyperliquid.service import EmptyParams, GetOrderbookParams
    limits = credentials.trading_limits
    validate_limits(limits)
    if reduce_only:
        return  # Venue-enforced reduce-only orders cannot increase the position.
    if not coin or ":" in coin or coin.startswith("@"):
        raise RiskLimitError("Exposure checks support native perpetual markets only")
    state = await tool.get_account_state(EmptyParams())
    orders = await tool.get_open_orders(EmptyParams())
    if state.get("success") is not True or orders.get("success") is not True:
        raise RiskLimitError("Cannot verify positions and open orders")
    positions = [{"market_id": r["coin"], "value": r["position_value"]}
                 for r in state["positions"]]
    pending = [{**r, "market_id": r["coin"]} for r in orders["orders"]]
    reported_total = nonnegative(state["total_ntl_pos"])
    measured_total = sum(nonnegative(r["value"]) for r in positions)
    if abs(reported_total - measured_total) > 0.01:
        raise RiskLimitError("Position list and total exposure disagree")
    # The aggregate must be readable even when the position list is empty.
    if nonnegative(state["total_ntl_pos"]) + nonnegative(notional) > limits.max_total_exposure_usd:
        raise RiskLimitError("Total exposure cap would be exceeded")
    check_exposure(limits, positions, pending, coin, notional)
    check_book(limits, await tool.get_orderbook(GetOrderbookParams(coin=coin, depth=20)))
    await tool.rate_limit("active_asset_limits")
    response = await tool._http_client.post(
        f"{credentials.api_url}/info", json={"type": "activeAssetData",
        "user": tool._resolve_query_address(credentials), "coin": coin})
    response.raise_for_status()
    asset = response.json()
    if asset["coin"] != coin or asset["user"].lower() != tool._resolve_query_address(credentials).lower():
        raise RiskLimitError("Active asset response does not match the trading account")
    leverage = nonnegative(asset["leverage"]["value"])
    if leverage < 1 or leverage > nonnegative(limits.max_leverage):
        raise RiskLimitError("Live leverage exceeds the configured maximum")


async def hyperliquid_daily_loss(tool, credentials, reduce_only):
    from datetime import datetime, timezone
    from polyrob_markets.hyperliquid.service import EmptyParams, GetFillsParams
    if reduce_only:
        return
    cap = nonnegative(credentials.trading_limits.max_daily_loss_usd)
    now = datetime.now(timezone.utc)
    day_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    fills = await tool.get_fills(GetFillsParams(limit=500))
    state = await tool.get_account_state(EmptyParams())
    if fills.get("success") is not True or state.get("success") is not True:
        raise RiskLimitError("Cannot verify daily loss from fills and positions")
    rows = fills["fills"]
    times = [nonnegative(r["time"]) for r in rows]
    if len(rows) >= 500 and min(times) >= day_start:
        raise RiskLimitError("Daily fills are truncated; cannot verify the daily-loss stop")
    pnl = 0.0
    for row, timestamp in zip(rows, times):
        if timestamp < day_start:
            continue
        if row["fee_token"] != "USDC":
            raise RiskLimitError("Cannot value fill fees for the daily-loss stop")
        pnl += float(row["closed_pnl"]) - float(row["fee"])
    pnl += sum(float(r["unrealized_pnl"]) for r in state["positions"])
    if not math.isfinite(pnl):
        raise RiskLimitError("Cannot verify non-finite daily P&L")
    if -pnl >= cap:
        raise RiskLimitError("Daily-loss cap reached; refusing to open new positions")
