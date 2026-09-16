"""D4 — the fields that decide the verdict arrive free and were dropped.

GeckoTerminal's pool payload already carries `transactions` (buys, sells,
unique buyers, unique sellers for m5/m15/m30/h1/h6/h24), `market_cap_usd` and
`fdv_usd`. `PoolCandidate` kept only name/dex/created/liquidity/volume/change,
so the three separators the prod research actually measured — volume over
liquidity, transactions per unique buyer, and market cap against liquidity —
could not be computed from a discovery call at all.
"""
import pytest

from tools.defi.providers.geckoterminal import PoolCandidate, parse_pools


def _payload(**attr_over):
    attrs = {
        "address": "0xpool",
        "name": "CASHCAT / USDG",
        "pool_created_at": "2026-07-01T12:00:00Z",
        "reserve_in_usd": "2230000",
        "volume_usd": {"h24": "3360000", "h1": "90000"},
        "price_change_percentage": {"h24": "3.2"},
        "market_cap_usd": "157000000",
        "fdv_usd": "160000000",
        "transactions": {
            "h1": {"buys": 61, "sells": 32, "buyers": 35, "sellers": 18},
            "h24": {"buys": 3922, "sells": 4031, "buyers": 739, "sellers": 700},
        },
    }
    attrs.update(attr_over)
    return {"data": [{
        "attributes": attrs,
        "relationships": {
            "base_token": {"data": {"id": "robinhood_0x020bfc650a365f8bb26819deaabf3e21291018b4"}},
            "dex": {"data": {"id": "pons-v2"}},
        },
    }]}


def _one(**over):
    return parse_pools(_payload(**over), "robinhood", "robinhood")[0]


def test_h24_trade_counts_are_carried():
    p = _one()
    assert p.trades_h24.buys == 3922
    assert p.trades_h24.sells == 4031
    assert p.trades_h24.buyers == 739


def test_h1_trade_counts_are_carried():
    assert _one().trades_h1.buys == 61


def test_market_cap_and_fdv_are_carried():
    p = _one()
    assert p.market_cap_usd == pytest.approx(157_000_000.0)
    assert p.fdv_usd == pytest.approx(160_000_000.0)


def test_volume_over_liquidity_is_computed():
    # 3.36M / 2.23M = 1.5 — the winner band. No wash case measured below 13.
    assert _one().vol_liq_ratio == pytest.approx(1.506, abs=0.01)


def test_volume_over_liquidity_is_unknown_without_liquidity():
    assert _one(reserve_in_usd="-1").vol_liq_ratio is None


def test_txns_per_unique_buyer_is_computed():
    # (3922 + 4031) / 739 = 10.8 — winners stayed under 15, bot churn ran 25-32.
    assert _one().txns_per_buyer == pytest.approx(10.76, abs=0.01)


def test_txns_per_buyer_is_unknown_with_no_buyers():
    p = _one(transactions={"h24": {"buys": 10, "sells": 10, "buyers": 0}})
    assert p.txns_per_buyer is None


def test_age_hours_is_computed_from_creation():
    from datetime import datetime, timezone, timedelta
    created = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    assert 49 < _one(pool_created_at=created).age_hours < 51


def test_age_hours_is_unknown_when_creation_is_missing():
    assert _one(pool_created_at=None).age_hours is None


def test_a_payload_without_transactions_reports_unknown_not_zero():
    p = _one(transactions={})
    assert p.trades_h24 is None
    assert p.txns_per_buyer is None


def test_hourly_volume_over_liquidity_is_computed():
    # 90k/hour against 2.23M liquidity = 0.04. Above 5 means wash in progress.
    assert _one().hourly_vol_liq_ratio == pytest.approx(0.0403, abs=0.001)


def test_mcap_over_liquidity_is_computed():
    assert _one().mcap_liq_ratio == pytest.approx(70.4, abs=0.5)


def test_a_zero_market_cap_is_unknown_not_zero():
    """Observed live: the indexer sent market_cap_usd 0 for a pool holding
    $77k of liquidity and $5.7M of daily volume. A token with a live pool does
    not have a market cap of zero — the indexer has not computed one, which is
    the same class of artifact as the negative reserve this module already
    treats as unknown."""
    p = _one(market_cap_usd="0")
    assert p.market_cap_usd is None
    assert p.mcap_liq_ratio is None


def test_a_zero_fdv_is_unknown_not_zero():
    assert _one(fdv_usd="0").fdv_usd is None


def test_a_real_market_cap_still_reads():
    assert _one().market_cap_usd == pytest.approx(157_000_000.0)
