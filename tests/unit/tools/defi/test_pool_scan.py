"""N3 — the composite verdict, from the measured thresholds.

Prod built this as a standalone script in a session workspace
(`tools/rh-scanner.py`), outside the repo, outside deploy and outside CI, from
a study of 5 survivors against 7 wash cases on Robinhood chain. The separators,
in power order: volume over liquidity (winners 0.06-2.1, wash 13-124 — a full
order of magnitude gap), liquidity depth (winners >= $1.4M, wash <= $258k),
pool age (winners 44-74 days, wash under 48 hours), and transactions per unique
buyer (winners under 15, bot ping-pong 25-32).

The one thing the workspace script got wrong is pinned here: it computed
`vl = inf` when liquidity was zero or unknown and then called the pool WASH.
An unknown denominator is not evidence of anything, and a verdict built on one
is a guess wearing a measurement's clothes.
"""
import pytest

from tools.defi.pool_screen import (
    STOCK_QUOTE_SYMBOLS, Verdict, classify, is_stock_pair,
)
from tools.defi.providers.geckoterminal import PoolCandidate, PoolTrades


def _pool(**over):
    d = dict(chain="robinhood", pool_address="0xp", base_token="0xt",
             name="CASHCAT / USDG", dex="pons-v2",
             created_at=None, liquidity_usd=2_230_000.0,
             volume_h24_usd=3_360_000.0, price_change_h24_pct=3.2,
             volume_h1_usd=90_000.0, market_cap_usd=157_000_000.0, fdv_usd=None,
             trades_h24=PoolTrades(buys=3922, sells=4031, buyers=739, sellers=700),
             trades_h1=None)
    d.update(over)
    return PoolCandidate(**d)


def _aged(hours):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# --- the survivor / wash split -------------------------------------------

def test_the_measured_winner_is_a_survivor():
    v = classify(_pool(created_at=_aged(74 * 24)))
    assert v.verdict == "SURVIVOR"


def test_a_deep_pool_younger_than_two_days_is_a_candidate_not_a_survivor():
    v = classify(_pool(created_at=_aged(20)))
    assert v.verdict == "CANDIDATE"
    assert any("48" in r for r in v.reasons)


def test_the_measured_wash_case_is_wash():
    """ANTHROPIG: $258k liquidity carrying $22.2M of 24h volume — V/L 86."""
    v = classify(_pool(created_at=_aged(30), liquidity_usd=258_000.0,
                       volume_h24_usd=22_200_000.0,
                       trades_h24=PoolTrades(buys=14641, sells=3768, buyers=570)))
    assert v.verdict == "WASH"
    assert any("V/L" in r for r in v.reasons)


def test_bot_ping_pong_is_wash_even_at_a_survivable_ratio():
    v = classify(_pool(created_at=_aged(200),
                       trades_h24=PoolTrades(buys=8000, sells=8000, buyers=500)))
    assert v.verdict == "WASH"
    assert any("per unique buyer" in r.lower() for r in v.reasons)


def test_a_wash_in_progress_is_caught_on_the_hourly_figure():
    """The 24h ratio smears out a wash that started an hour ago."""
    v = classify(_pool(created_at=_aged(200), volume_h1_usd=20_000_000.0))
    assert v.verdict == "WASH"
    assert any("1h" in r for r in v.reasons)


def test_a_thin_pool_is_a_pass_not_a_wash():
    """Shallow is a reason not to size into it, not evidence of manipulation."""
    v = classify(_pool(created_at=_aged(200), liquidity_usd=20_000.0,
                       volume_h24_usd=20_000.0))
    assert v.verdict == "PASS"


def test_a_pool_under_twelve_hours_is_too_new_not_a_verdict():
    v = classify(_pool(created_at=_aged(3), liquidity_usd=600_000.0,
                       volume_h24_usd=600_000.0))
    assert v.verdict == "TOO_NEW"


# --- unknowns never become verdicts --------------------------------------

def test_unknown_liquidity_is_unscreenable_never_wash():
    """The workspace script divided by a missing denominator and called the
    result WASH. An indexer that has not caught up is not a manipulator."""
    v = classify(_pool(created_at=_aged(200), liquidity_usd=None))
    assert v.verdict == "UNSCREENABLE"
    assert any("liquidity" in u for u in v.unknowns)


def test_unknown_volume_is_unscreenable():
    v = classify(_pool(created_at=_aged(200), volume_h24_usd=None))
    assert v.verdict == "UNSCREENABLE"


def test_unknown_age_is_named_but_does_not_block_a_wash_call():
    """A wash signature is visible without knowing the pool's age."""
    v = classify(_pool(created_at=None, liquidity_usd=258_000.0,
                       volume_h24_usd=22_200_000.0))
    assert v.verdict == "WASH"
    assert any("age" in u.lower() for u in v.unknowns)


def test_unknown_age_downgrades_a_survivor_to_candidate():
    v = classify(_pool(created_at=None))
    assert v.verdict == "CANDIDATE"
    assert any("age" in u.lower() for u in v.unknowns)


def test_missing_trade_counts_are_named_not_treated_as_clean():
    v = classify(_pool(created_at=_aged(200), trades_h24=None))
    assert v.verdict == "SURVIVOR"
    assert any("buyer" in u.lower() for u in v.unknowns)


def test_every_verdict_states_at_least_one_reason():
    for pool in (_pool(created_at=_aged(200)), _pool(created_at=_aged(3)),
                 _pool(created_at=None, liquidity_usd=None)):
        assert classify(pool).reasons


# --- N4: the stock-pair tag ----------------------------------------------

def test_a_meme_paired_against_a_tokenized_stock_is_tagged():
    assert is_stock_pair("BANGERCAT / NVDA") is True
    assert classify(_pool(name="BANGERCAT / NVDA", created_at=_aged(200))).stock_pair is True


def test_an_ordinary_quote_asset_is_not_tagged():
    assert is_stock_pair("CASHCAT / USDG") is False
    assert is_stock_pair("MEME / WETH") is False


def test_the_stock_quote_list_is_matched_on_the_quote_side_only():
    """A token NAMED after a stock is not a stock pair; the QUOTE side is."""
    assert is_stock_pair("NVDA-INU / WETH") is False


def test_the_stock_quote_set_is_not_empty():
    assert "NVDA" in STOCK_QUOTE_SYMBOLS and "SPY" in STOCK_QUOTE_SYMBOLS
