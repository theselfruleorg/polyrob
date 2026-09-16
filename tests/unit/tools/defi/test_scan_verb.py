"""The `scan` verb: discovery, classified, ordered, with nothing hidden."""
import pytest

from tools.defi.data_tool import DefiDataTool, ScanParams
from tools.defi.providers.geckoterminal import PoolCandidate, PoolTrades


def _text(res):
    return (res.extracted_content or "") + (res.error or "")


def _aged(hours):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _pool(name="CASHCAT / USDG", **over):
    d = dict(chain="robinhood", pool_address="0xp", base_token="0xt", name=name,
             dex="pons-v2", created_at=_aged(200), liquidity_usd=2_230_000.0,
             volume_h24_usd=3_360_000.0, price_change_h24_pct=3.2,
             volume_h1_usd=90_000.0, market_cap_usd=157_000_000.0, fdv_usd=None,
             trades_h24=PoolTrades(buys=3922, sells=4031, buyers=739), trades_h1=None)
    d.update(over)
    return PoolCandidate(**d)


def _wash():
    return _pool(name="ANTHROPIG / WETH", created_at=_aged(30),
                 liquidity_usd=258_000.0, volume_h24_usd=22_200_000.0,
                 volume_h1_usd=1_000.0,
                 trades_h24=PoolTrades(buys=14641, sells=3768, buyers=570))


@pytest.mark.asyncio
async def test_scan_states_a_verdict_per_pool():
    tool = DefiDataTool(discover_fn=lambda c, k: [_pool(), _wash()])
    out = _text(await tool.scan(ScanParams(chain="robinhood")))
    assert "SURVIVOR" in out and "WASH" in out


@pytest.mark.asyncio
async def test_scan_states_the_reason_for_each_verdict():
    tool = DefiDataTool(discover_fn=lambda c, k: [_wash()])
    out = _text(await tool.scan(ScanParams(chain="robinhood")))
    assert "V/L" in out


@pytest.mark.asyncio
async def test_scan_shows_wash_rows_rather_than_dropping_them():
    """A silent filter reads as 'this is all there was'. Knowing which pools
    are being wash-traded IS the information."""
    tool = DefiDataTool(discover_fn=lambda c, k: [_wash()])
    out = _text(await tool.scan(ScanParams(chain="robinhood")))
    assert "ANTHROPIG" in out


@pytest.mark.asyncio
async def test_scan_tags_a_tokenized_stock_pair():
    tool = DefiDataTool(discover_fn=lambda c, k: [_pool(name="BANGERCAT / NVDA")])
    out = _text(await tool.scan(ScanParams(chain="robinhood")))
    assert "stock" in out.lower()


@pytest.mark.asyncio
async def test_scan_counts_each_verdict():
    tool = DefiDataTool(discover_fn=lambda c, k: [_pool(), _pool(), _wash()])
    out = _text(await tool.scan(ScanParams(chain="robinhood")))
    assert "SURVIVOR 2" in out or "2 SURVIVOR" in out


@pytest.mark.asyncio
async def test_scan_says_a_verdict_is_not_a_safety_screen():
    tool = DefiDataTool(discover_fn=lambda c, k: [_pool()])
    out = _text(await tool.scan(ScanParams(chain="robinhood")))
    assert "token_info" in out


@pytest.mark.asyncio
async def test_scan_surfaces_what_could_not_be_checked():
    tool = DefiDataTool(discover_fn=lambda c, k: [_pool(trades_h24=None)])
    out = _text(await tool.scan(ScanParams(chain="robinhood")))
    assert "not checked" in out.lower() or "unknown" in out.lower()


@pytest.mark.asyncio
async def test_an_unsupported_chain_is_refused():
    res = await DefiDataTool(discover_fn=lambda c, k: []).scan(ScanParams(chain="sui"))
    assert res.error and "not supported" in res.error


@pytest.mark.asyncio
async def test_an_empty_scan_says_so_rather_than_looking_clean():
    tool = DefiDataTool(discover_fn=lambda c, k: [])
    out = _text(await tool.scan(ScanParams(chain="robinhood")))
    assert "no pools" in out.lower()


@pytest.mark.asyncio
async def test_a_failing_indexer_is_an_error_not_an_empty_scan():
    def _boom(c, k):
        raise RuntimeError("429")

    res = await DefiDataTool(discover_fn=_boom).scan(ScanParams(chain="robinhood"))
    assert res.error
