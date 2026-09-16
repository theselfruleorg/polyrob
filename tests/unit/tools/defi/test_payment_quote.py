"""The tools-tier quoter reads the DEEPEST pool and classifies THAT pool."""
from core.payments.assets import PaymentAsset
from tools.defi.payment_quote import PaymentQuoter, _deepest_priced_pool

ASSET = PaymentAsset(asset_id="rob", chain="robinhood", address="0x" + "bb" * 20,
                     decimals=18, symbol="ROB", min_amount_raw=10 ** 18,
                     liquidity_floor_usd=2500.0)


class _Pool:
    """The shape `pool_screen.classify` reads."""
    liquidity_usd = 50_000.0
    volume_h24_usd = 60_000.0
    volume_h1_usd = 2_000.0
    created_at = "2020-01-01T00:00:00Z"
    name = "ROB/WETH"
    vol_liq_ratio = 1.2
    hourly_vol_liq_ratio = 0.04
    mcap_liq_ratio = None
    txns_per_buyer = None
    age_hours = 4000.0


def test_the_quote_carries_the_screen_verdict_and_the_pool_depth():
    q = PaymentQuoter(pool_fn=lambda c, a: (_Pool(), 0.10)).quote(ASSET)
    assert q.usd_per_token == 0.10
    assert q.liquidity_usd == 50_000.0
    assert q.verdict
    assert q.asset_id == "rob"
    assert q.source == "geckoterminal"


def test_no_pool_yields_no_quote_never_a_zero_price():
    """⚠️ A missing quote is a REFUSAL upstream, never a free action."""
    assert PaymentQuoter(pool_fn=lambda c, a: None).quote(ASSET) is None


def test_a_raising_indexer_yields_no_quote_and_does_not_propagate():
    def boom(chain, addr):
        raise RuntimeError("indexer down")
    assert PaymentQuoter(pool_fn=boom).quote(ASSET) is None


def test_a_native_asset_with_no_address_yields_no_quote():
    native = PaymentAsset(asset_id="eth", chain="base", address=None,
                          decimals=18, symbol="ETH")
    assert PaymentQuoter(pool_fn=lambda c, a: (_Pool(), 1.0)).quote(native) is None


def _payload(*pools):
    return {"data": [{"attributes": dict(p)} for p in pools]}


def test_the_deepest_pool_wins_and_its_own_price_is_used():
    """⚠️ A token's pools disagree, and the thin one is the one an attacker
    seeded. Pricing against one pool while screening another would let the
    screen bless liquidity the price never touched."""
    payload = _payload(
        {"address": "0xthin", "reserve_in_usd": "900",
         "base_token_price_usd": "99.0", "name": "thin",
         "volume_usd": {"h24": "10"}, "price_change_percentage": {"h24": "0"}},
        {"address": "0xdeep", "reserve_in_usd": "50000",
         "base_token_price_usd": "0.10", "name": "deep",
         "volume_usd": {"h24": "60000"}, "price_change_percentage": {"h24": "0"}},
    )
    pool, price = _deepest_priced_pool(
        "base", "0x" + "bb" * 20, fetch=lambda url: payload)
    assert pool.pool_address == "0xdeep"
    assert price == 0.10


def test_a_pool_with_no_price_is_not_a_price():
    payload = _payload({"address": "0xdeep", "reserve_in_usd": "50000",
                        "name": "deep", "volume_usd": {"h24": "1"},
                        "price_change_percentage": {"h24": "0"}})
    assert _deepest_priced_pool(
        "base", "0x" + "bb" * 20, fetch=lambda url: payload) is None


def test_an_unindexed_chain_yields_nothing():
    assert _deepest_priced_pool("atlantis", "0x" + "bb" * 20,
                                fetch=lambda url: {}) is None


def test_a_pool_with_no_liquidity_figure_is_skipped():
    """A missing reserve is UNKNOWN, not zero — and an unknown depth cannot be
    the deepest pool."""
    payload = _payload({"address": "0xa", "name": "a",
                        "base_token_price_usd": "1.0",
                        "volume_usd": {"h24": "1"},
                        "price_change_percentage": {"h24": "0"}})
    assert _deepest_priced_pool(
        "base", "0x" + "bb" * 20, fetch=lambda url: payload) is None
