"""Revalidation pass over the 2026-09-14 work. Each test is a found defect.

None of these came from a failing suite — the suite was green. They came from
asking, of each new surface, "what does this trust that it did not check?"
"""
import pytest

from tools.defi.data_tool import DefiDataTool, OhlcvParams
from tools.defi.providers import geckoterminal as gt

POOL = "0xa70fc67c9f69da90b63a0e4c05d229954574e313"
TOKEN = "0x020bfC650A365f8BB26819deAAbF3E21291018b4"


def _text(res):
    return (res.extracted_content or "") + (res.error or "")


# --- 1. the ohlcv `pool` parameter went into a URL PATH unchecked ----------

def test_a_pool_that_is_not_an_address_is_refused_by_the_provider():
    """`pool` was interpolated straight into the indexer URL, so a value like
    `../../../networks/eth/trending_pools?x=` re-pointed the request at a
    different endpoint on the same host — and its answer would have been
    rendered as THIS token's price history. The agent reads web pages, so a
    "pool address" it is told is attacker-influenced input on a money path."""
    with pytest.raises(ValueError):
        gt.ohlcv("base", "../../../networks/eth/trending_pools?x=", fetch=lambda u: {})


def test_a_pool_with_a_query_separator_is_refused():
    with pytest.raises(ValueError):
        gt.ohlcv("base", f"{POOL}?aggregate=999", fetch=lambda u: {})


def test_a_pool_with_a_path_separator_is_refused():
    with pytest.raises(ValueError):
        gt.ohlcv("base", f"{POOL}/ohlcv/day", fetch=lambda u: {})


def test_a_real_pool_address_still_builds_the_expected_url():
    seen = {}

    def fetch(url):
        seen["url"] = url
        return {}

    gt.ohlcv("base", POOL, timeframe="hour", aggregate=1, limit=5, fetch=fetch)
    assert seen["url"] == (
        "https://api.geckoterminal.com/api/v2/networks/base/pools/"
        f"{POOL}/ohlcv/hour?aggregate=1&limit=5")


def test_a_solana_pool_address_is_accepted():
    seen = {}
    gt.ohlcv("solana", "9ivuoJSHNHJ126m5TsH3HRXKovHx9M1c43KH26FmBpqh",
             fetch=lambda u: seen.setdefault("url", u) and {})
    assert "9ivuoJSHNHJ126m5TsH3HRXKovHx9M1c43KH26FmBpqh" in seen["url"]


def test_top_pool_for_token_also_refuses_a_non_address():
    with pytest.raises(ValueError):
        gt.top_pool_for_token("base", "../../x", fetch=lambda u: {})


@pytest.mark.asyncio
async def test_the_verb_refuses_a_malformed_pool_before_any_fetch():
    called = []
    tool = DefiDataTool(
        pool_for_token_fn=lambda c, a: POOL,
        ohlcv_fn=lambda c, p, **kw: called.append(1) or [])
    res = await tool.ohlcv(OhlcvParams(
        chain="base", address=TOKEN, pool="../../../networks/eth/trending_pools"))
    assert res.error and not called


@pytest.mark.asyncio
async def test_the_verb_still_accepts_a_real_pool():
    tool = DefiDataTool(ohlcv_fn=lambda c, p, **kw: [])
    out = _text(await tool.ohlcv(OhlcvParams(chain="base", address=TOKEN, pool=POOL)))
    assert "no candles" in out.lower()


# --- 2. a figure that is not a number must render `unknown` ---------------

def test_a_nan_price_renders_unknown_not_a_number():
    """A corrupted feed yielding NaN rendered `$nan`, which reads as a figure.
    NaN IS the canonical 'not a number' — the honest rendering is `unknown`,
    the same word every other unreadable figure gets."""
    from tools.defi.data_tool import _fmt_usd
    assert _fmt_usd(float("nan")) == "unknown"


def test_an_infinite_price_renders_unknown():
    from tools.defi.data_tool import _fmt_usd
    assert _fmt_usd(float("inf")) == "unknown"
    assert _fmt_usd(float("-inf")) == "unknown"


def test_nan_percentages_hours_and_ratios_render_unknown():
    from tools.defi.data_tool import _fmt_pct, _fmt_ratio, _fmt_hours
    for fn in (_fmt_pct, _fmt_ratio, _fmt_hours):
        assert fn(float("nan")) == "unknown", fn.__name__
        assert fn(float("inf")) == "unknown", fn.__name__


# --- 3. the sub-cent branch could still round to zero --------------------

def test_an_extremely_small_price_never_renders_as_zero():
    """The sub-cent fix caps at 18 decimal places, so a value below that
    stripped back to `$0` — reintroducing the confident zero the fix existed to
    remove, for the one input where it is hardest to notice."""
    from tools.defi.data_tool import _fmt_usd
    out = _fmt_usd(1e-300)
    assert out != "$0"
    assert "0" != out.lstrip("$")
    assert "e-" in out


def test_a_true_zero_is_still_exactly_zero():
    from tools.defi.data_tool import _fmt_usd
    assert _fmt_usd(0.0) == "$0.00"
    assert _fmt_usd(-0.0) == "$0.00"


# --- 4. a derived ratio must refuse a non-positive denominator -----------

def test_a_ratio_over_negative_liquidity_is_unknown_not_a_negative_ratio():
    """`_positive()` keeps a negative reserve out of the parser, but the
    dataclass is public and a negative denominator silently produced a negative
    V/L that then classified as PASS."""
    from tools.defi.providers.geckoterminal import PoolCandidate
    p = PoolCandidate(chain="base", pool_address="0xp", base_token=None,
                      name="A / B", dex="d", created_at=None,
                      liquidity_usd=-5.0, volume_h24_usd=1.0,
                      price_change_h24_pct=None, volume_h1_usd=1.0,
                      market_cap_usd=1.0)
    assert p.vol_liq_ratio is None
    assert p.hourly_vol_liq_ratio is None
    assert p.mcap_liq_ratio is None
