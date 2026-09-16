"""Sizing a USD price into a volatile token — and every reason to refuse."""
import pytest

from core.payments.assets import PaymentAsset
from core.payments.quote import PriceQuote, QuoteRefused, size_amount_raw

ASSET = PaymentAsset(asset_id="rob", chain="robinhood", address="0x" + "bb" * 20,
                     decimals=18, symbol="ROB", rail="onchain_scan",
                     min_amount_raw=10 ** 18, liquidity_floor_usd=2500.0,
                     verified_at=1.0, source="operator")
USDC = PaymentAsset(asset_id="usdc-base", chain="base",
                    address="0x" + "aa" * 20, decimals=6, symbol="USDC",
                    rail="facilitator")


def _q(**kw):
    base = dict(asset_id="rob", usd_per_token=0.10, liquidity_usd=50_000.0,
                verdict="SURVIVOR", source="test", ts=1000.0)
    base.update(kw)
    return PriceQuote(**base)


def test_a_healthy_quote_sizes_the_usd_price_into_raw_units():
    # $0.50 at $0.10/token = 5 tokens = 5e18 raw
    assert size_amount_raw(0.50, ASSET, _q(), now=1000.0) == 5 * 10 ** 18


def test_a_stablecoin_needs_no_quote_at_all():
    assert size_amount_raw(1.25, USDC, None) == 1_250_000


def test_no_quoter_for_a_volatile_asset_is_a_refusal_not_a_guess():
    with pytest.raises(QuoteRefused, match="no price"):
        size_amount_raw(0.50, ASSET, None)


def test_a_stale_quote_is_refused():
    with pytest.raises(QuoteRefused, match="stale"):
        size_amount_raw(0.50, ASSET, _q(ts=0.0), now=1000.0, max_age_sec=300)


@pytest.mark.parametrize("verdict", ["WASH", "UNSCREENABLE"])
def test_a_wash_or_unscreenable_pool_is_refused(verdict):
    """⚠️ UNSCREENABLE too: an indexer that has not caught up is not a
    manipulator, but it is also not a price we may charge against — and the two
    are indistinguishable from here."""
    with pytest.raises(QuoteRefused, match=verdict):
        size_amount_raw(0.50, ASSET, _q(verdict=verdict), now=1000.0)


def test_liquidity_below_the_assets_floor_is_refused():
    with pytest.raises(QuoteRefused, match="liquidity"):
        size_amount_raw(0.50, ASSET, _q(liquidity_usd=100.0), now=1000.0)


def test_a_pumped_price_cannot_take_the_fee_below_the_token_floor():
    """⚠️ THE attack. Move a thin pool UP and the USD fee shrinks to dust in
    token terms: $0.50 at $1000/token is 0.0005 tokens. Below the 1-token floor,
    so it REFUSES rather than selling a near-free mute."""
    with pytest.raises(QuoteRefused, match="floor"):
        size_amount_raw(0.50, ASSET, _q(usd_per_token=1000.0), now=1000.0)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_zero_or_negative_token_price_is_refused(bad):
    with pytest.raises(QuoteRefused):
        size_amount_raw(0.50, ASSET, _q(usd_per_token=bad), now=1000.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, None])
def test_a_zero_or_negative_usd_price_is_never_treated_as_free(bad):
    with pytest.raises(QuoteRefused):
        size_amount_raw(bad, ASSET, _q(), now=1000.0)


def test_a_crashed_price_still_sizes_and_is_bounded_by_the_caller():
    """A LOWER price makes the fee larger in token units. That only costs the
    payer, and X402_INVOICE_MAX_USD already bounds the USD side."""
    assert size_amount_raw(0.50, ASSET, _q(usd_per_token=0.001),
                           now=1000.0) == 500 * 10 ** 18


def test_an_amount_that_rounds_to_zero_raw_units_is_refused():
    tiny = PaymentAsset(asset_id="t", chain="base", address="0x" + "cc" * 20,
                        decimals=0, symbol="TINY")
    with pytest.raises(QuoteRefused, match="zero raw units"):
        size_amount_raw(0.4, tiny, _q(usd_per_token=1.0), now=1000.0)


def test_the_floor_is_enforced_for_a_STABLE_asset_too():
    """An operator can put a minimum on USDC as well — the floor is a property
    of the asset row, not of the quoting path."""
    floored = PaymentAsset(asset_id="usdc-base", chain="base",
                           address="0x" + "aa" * 20, decimals=6, symbol="USDC",
                           min_amount_raw=5_000_000)
    with pytest.raises(QuoteRefused, match="floor"):
        size_amount_raw(1.0, floored, None)
