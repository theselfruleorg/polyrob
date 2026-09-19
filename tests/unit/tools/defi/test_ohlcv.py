"""N2 — price history. Candles, and the shape read a 24h snapshot cannot give.

The agent named this its own second-biggest weakness: "All timing decisions use
24h snapshots. Can't distinguish 'steady climber' from 'one spike 20h ago.'"
The measured research turns on exactly that: winners climbed over ~40 days in
steps, wash tokens peaked in 2-6 hours and lost 75-99%.

Candles are POOL-scoped on this indexer, not token-scoped. Handing it a token
address returns nothing, which is why the verb resolves the token's deepest pool
and SAYS which pool it read.
"""
import pytest

from tools.defi.providers.geckoterminal import Candle, parse_ohlcv
from tools.defi.data_tool import DefiDataTool, OhlcvParams

TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
POOL = "0xd42a491087a15e5afd51feb3606066cc152d2b09"


def _payload(rows):
    return {"data": {"attributes": {"ohlcv_list": rows}}}


def _text(res):
    return (res.extracted_content or "") + (res.error or "")


# --- the parser -----------------------------------------------------------

def test_candles_are_parsed_oldest_first():
    out = parse_ohlcv(_payload([
        [1789365600, 2.0, 3.0, 1.5, 2.5, 100.0],
        [1789362000, 1.0, 1.2, 0.9, 1.1, 50.0],
    ]))
    assert [c.timestamp for c in out] == [1789362000, 1789365600]
    assert out[0].open == pytest.approx(1.0)
    assert out[-1].close == pytest.approx(2.5)


def test_a_malformed_row_is_dropped_not_zeroed():
    out = parse_ohlcv(_payload([[1, 1.0, 1.0, 1.0, 1.0, 1.0], ["bad"], None]))
    assert len(out) == 1


def test_an_empty_payload_is_an_empty_list():
    assert parse_ohlcv({}) == []
    assert parse_ohlcv(None) == []


# --- the shape read -------------------------------------------------------

def _series(closes, start=1_000_000, step=3600):
    return [Candle(start + i * step, c, c, c, c, 1000.0) for i, c in enumerate(closes)]


def test_peak_and_time_to_peak_are_measured():
    from tools.defi.providers.geckoterminal import summarize_candles
    s = summarize_candles(_series([1.0, 2.0, 50.0, 20.0, 6.0]))
    assert s.peak == pytest.approx(50.0)
    assert s.candles_to_peak == 2
    assert s.pct_off_peak == pytest.approx(-88.0, abs=0.5)


def test_a_vertical_then_dumped_series_is_described_as_such():
    """The measured wash shape: peak at hour 4-5, then 30 hours of decline."""
    from tools.defi.providers.geckoterminal import summarize_candles
    s = summarize_candles(_series(
        [0.0004, 0.004, 0.0198, 0.008, 0.0026, 0.0021, 0.0018,
         0.0016, 0.0015, 0.0014, 0.0013, 0.0012]))
    assert s.pct_off_peak < -80
    assert s.peak_in_first_half is True
    assert s.candles_to_peak == 2


def test_a_stair_step_climber_peaks_late():
    from tools.defi.providers.geckoterminal import summarize_candles
    s = summarize_candles(_series([1.0, 1.4, 1.3, 1.9, 1.8, 2.6, 2.5, 3.4]))
    assert s.peak_in_first_half is False
    assert s.pct_off_peak > -20


def test_an_empty_series_summarizes_to_unknown_not_zero():
    from tools.defi.providers.geckoterminal import summarize_candles
    s = summarize_candles([])
    assert s.peak is None and s.pct_off_peak is None


# --- the verb -------------------------------------------------------------

@pytest.mark.asyncio
async def test_ohlcv_renders_candles_and_the_shape():
    tool = DefiDataTool(
        pool_for_token_fn=lambda c, a: POOL,
        ohlcv_fn=lambda c, p, **kw: _series([1.0, 2.0, 50.0, 20.0]))
    out = _text(await tool.ohlcv(OhlcvParams(chain="base", address=TOKEN)))
    assert "50" in out
    assert "peak" in out.lower()


@pytest.mark.asyncio
async def test_ohlcv_names_the_pool_it_read():
    """Candles are pool-scoped. Not saying which pool makes the numbers
    unattributable, and a token's pools disagree."""
    tool = DefiDataTool(
        pool_for_token_fn=lambda c, a: POOL,
        ohlcv_fn=lambda c, p, **kw: _series([1.0, 2.0]))
    out = _text(await tool.ohlcv(OhlcvParams(chain="base", address=TOKEN)))
    assert POOL in out


@pytest.mark.asyncio
async def test_ohlcv_uses_an_explicit_pool_without_resolving():
    called = []
    tool = DefiDataTool(
        pool_for_token_fn=lambda c, a: called.append(1) or POOL,
        ohlcv_fn=lambda c, p, **kw: _series([1.0]))
    await tool.ohlcv(OhlcvParams(chain="base", address=TOKEN, pool=POOL))
    assert not called


@pytest.mark.asyncio
async def test_no_candles_is_stated_never_rendered_as_a_flat_chart():
    tool = DefiDataTool(pool_for_token_fn=lambda c, a: POOL,
                        ohlcv_fn=lambda c, p, **kw: [])
    out = _text(await tool.ohlcv(OhlcvParams(chain="base", address=TOKEN)))
    assert "no candles" in out.lower() or "no price history" in out.lower()
    assert "0.00" not in out


@pytest.mark.asyncio
async def test_an_unresolvable_pool_is_an_honest_error():
    tool = DefiDataTool(pool_for_token_fn=lambda c, a: None,
                        ohlcv_fn=lambda c, p, **kw: _series([1.0]))
    res = await tool.ohlcv(OhlcvParams(chain="base", address=TOKEN))
    assert res.error and "pool" in res.error.lower()


@pytest.mark.asyncio
async def test_bad_address_is_refused_before_any_provider_call():
    called = []
    tool = DefiDataTool(pool_for_token_fn=lambda c, a: called.append(1) or POOL,
                        ohlcv_fn=lambda c, p, **kw: _series([1.0]))
    res = await tool.ohlcv(OhlcvParams(chain="base", address="nope"))
    assert res.error and not called


@pytest.mark.asyncio
async def test_timeframe_is_passed_through():
    seen = {}

    def _fetch(chain, pool, timeframe=None, aggregate=None, limit=None):
        seen.update(timeframe=timeframe, aggregate=aggregate, limit=limit)
        return _series([1.0])

    tool = DefiDataTool(pool_for_token_fn=lambda c, a: POOL, ohlcv_fn=_fetch)
    await tool.ohlcv(OhlcvParams(chain="base", address=TOKEN,
                                 timeframe="day", aggregate=1, limit=30))
    assert seen["timeframe"] == "day" and seen["limit"] == 30


# --- 2026-09-19 (Rob's self-review ask #3): Robinhood Chain pools are bytes32 ids ----
# Singleton-style AMMs on Robinhood Chain identify a pool by a 32-byte id, and that
# is what the indexer returns as the pool "address". The path validator treated a
# pool id as "an address on its chain" (20 bytes), so every RH candle read — the
# auto-resolved pool AND an explicit `pool=` — was refused and the track record
# marked R5 peak/trail NOT CHECKED on every row. A pool id may now also be a strict
# 0x + 64-hex bytes32 (still path-safe: hex only); a token address may not.

POOL32 = "0x" + "ab" * 32


def test_url_safe_pool_accepts_bytes32_and_token_does_not():
    from tools.defi.providers.geckoterminal import _url_safe_address
    assert _url_safe_address("robinhood", POOL32, what="pool") == POOL32
    with pytest.raises(ValueError):
        _url_safe_address("robinhood", POOL32, what="token")
    with pytest.raises(ValueError):  # not hex → still refused (path safety)
        _url_safe_address("robinhood", "0x" + "zz" * 32, what="pool")
    with pytest.raises(ValueError):  # wrong width
        _url_safe_address("robinhood", "0x" + "ab" * 31, what="pool")


@pytest.mark.asyncio
async def test_ohlcv_accepts_an_explicit_bytes32_pool():
    seen = []
    tool = DefiDataTool(
        pool_for_token_fn=lambda c, a: (_ for _ in ()).throw(AssertionError("must not resolve")),
        ohlcv_fn=lambda c, p, **kw: seen.append(p) or _series([1.0, 2.0]))
    res = await tool.ohlcv(OhlcvParams(chain="robinhood", address=TOKEN, pool=POOL32))
    assert res.error is None, res.error
    assert seen == [POOL32]
    assert POOL32[:10] in _text(res)


@pytest.mark.asyncio
async def test_ohlcv_accepts_a_resolved_bytes32_pool():
    tool = DefiDataTool(
        pool_for_token_fn=lambda c, a: POOL32,
        ohlcv_fn=lambda c, p, **kw: _series([1.0, 2.0]))
    res = await tool.ohlcv(OhlcvParams(chain="robinhood", address=TOKEN))
    assert res.error is None, res.error


# --- 2026-09-19: a 429 from the indexer is NOT "no indexed pool" ---------------------
# Goal dd8bd542d3f9 batched four candle reads in one step; GeckoTerminal answered 429 to
# five of them and the tool rendered "no indexed pool found … not an outage — the indexer
# has not caught up". That is a confident-wrong message: the indexer refused, it did not
# say the pool is absent. Now: `_get` retries once with a short backoff on 429, and a
# provider error that survives the retry PROPAGATES from `top_pool_for_token` (the action
# renders "could not resolve a pool: <429>") instead of collapsing to None.

class _RateLimited(Exception):
    def __init__(self):
        super().__init__("HTTP Error 429: Too Many Requests")
        self.code = 429


def test_top_pool_propagates_a_provider_error_instead_of_none():
    from tools.defi.providers.geckoterminal import top_pool_for_token
    def _fetch(url):
        raise _RateLimited()
    with pytest.raises(Exception) as ei:
        top_pool_for_token("robinhood", TOKEN, fetch=_fetch)
    assert "429" in str(ei.value)


def test_top_pool_returns_none_only_for_a_genuinely_empty_answer():
    from tools.defi.providers.geckoterminal import top_pool_for_token
    assert top_pool_for_token("robinhood", TOKEN, fetch=lambda url: {"data": []}) is None


def test_get_retries_once_on_429(monkeypatch):
    import importlib
    from tools.defi.providers import geckoterminal as gt
    # the autouse network guard swaps `_get` out; reload to get the REAL one and
    # exercise it against a stubbed transport (the stub keeps this network-free)
    src_get = importlib.reload(gt)._get
    calls = []
    def _get_json(url, *, timeout, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise _RateLimited()
        return {"ok": True}
    monkeypatch.setattr("tools.defi.providers._http.get_json", _get_json)
    monkeypatch.setattr(gt, "_RETRY_SLEEP_SEC", 0.0)
    assert src_get("https://x/y") == {"ok": True}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_ohlcv_names_the_indexer_refusal_not_an_absent_pool():
    def _resolve(c, a):
        raise _RateLimited()
    tool = DefiDataTool(pool_for_token_fn=_resolve, ohlcv_fn=lambda c, p, **kw: [])
    res = await tool.ohlcv(OhlcvParams(chain="robinhood", address=TOKEN))
    assert "429" in (res.error or "")
    assert "no indexed pool" not in _text(res)
