"""D3 — one index misses every fresh launch.

`token_resolve` searched Dexscreener and nothing else. Prod recorded it plainly:
"Resolver index lags ALL these launches - no chain-matching addresses for arb
DAWN/RHUBARB/ARBIOUS/NOLA or polygon LGNS/MitiW/NODAL. The resolver only knows
wrong-chain or established tokens." The agent's fix was to bypass the verb and
read GeckoTerminal itself, which is the second index this adds.
"""
import pytest

from tools.defi.data_tool import DefiDataTool, ResolveParams
from tools.defi.providers.base import Candidate
from tools.defi.providers.geckoterminal import parse_search_pools

FRESH = "0x020bfC650A365f8BB26819deAAbF3E21291018b4"
OLD = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _text(res):
    return (res.extracted_content or "") + (res.error or "")


def _cand(addr, liq, chain="base", symbol="CASHCAT"):
    return Candidate(chain=chain, address=addr, symbol=symbol, name="n",
                     liquidity_usd=liq, price_usd=1.0)


# --- the second index's parser -------------------------------------------

def _search_payload(**over):
    attrs = {"address": "0xpool", "name": "CASHCAT / USDG",
             "reserve_in_usd": "2230000", "base_token_price_usd": "0.154"}
    attrs.update(over)
    return {"data": [{
        "attributes": attrs,
        "relationships": {"base_token": {
            "data": {"id": f"robinhood_{FRESH.lower()}"}}},
    }]}


def test_search_pools_yields_a_candidate_with_its_chain_and_address():
    out = parse_search_pools(_search_payload())
    assert len(out) == 1
    assert out[0].chain == "robinhood"
    assert out[0].address.lower() == FRESH.lower()
    assert out[0].symbol == "CASHCAT"
    assert out[0].liquidity_usd == pytest.approx(2_230_000.0)


def test_search_pools_drops_a_row_whose_chain_we_do_not_cover():
    payload = {"data": [{"attributes": {"name": "X / Y"},
                         "relationships": {"base_token": {"data": {"id": "sui_0xabc"}}}}]}
    assert parse_search_pools(payload) == []


def test_search_pools_survives_a_missing_base_token():
    payload = {"data": [{"attributes": {"name": "X / Y"}, "relationships": {}}]}
    assert parse_search_pools(payload) == []


# --- the verb -------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_falls_back_to_the_second_index_when_the_first_is_empty():
    tool = DefiDataTool(search_fn=lambda s: [],
                        search2_fn=lambda s: [_cand(FRESH, 2_230_000.0)])
    out = _text(await tool.token_resolve(ResolveParams(symbol="CASHCAT")))
    assert FRESH in out


@pytest.mark.asyncio
async def test_resolve_merges_both_indexes_and_dedupes_by_address():
    tool = DefiDataTool(search_fn=lambda s: [_cand(FRESH, 1.0)],
                        search2_fn=lambda s: [_cand(FRESH, 2_230_000.0),
                                              _cand(OLD, 900.0)])
    out = _text(await tool.token_resolve(ResolveParams(symbol="CASHCAT")))
    assert out.count(FRESH) == 1, "the same contract must not be listed twice"
    assert OLD in out


@pytest.mark.asyncio
async def test_resolve_keeps_the_richer_liquidity_figure_on_a_duplicate():
    tool = DefiDataTool(search_fn=lambda s: [_cand(FRESH, 0.0)],
                        search2_fn=lambda s: [_cand(FRESH, 2_230_000.0)])
    out = _text(await tool.token_resolve(ResolveParams(symbol="CASHCAT")))
    assert "2,230,000" in out


@pytest.mark.asyncio
async def test_resolve_names_which_indexes_answered():
    tool = DefiDataTool(search_fn=lambda s: [], search2_fn=lambda s: [_cand(FRESH, 5.0)])
    out = _text(await tool.token_resolve(ResolveParams(symbol="X")))
    assert "geckoterminal" in out.lower()


@pytest.mark.asyncio
async def test_a_failing_second_index_does_not_lose_the_first():
    def _boom(_):
        raise RuntimeError("429")

    tool = DefiDataTool(search_fn=lambda s: [_cand(OLD, 900.0)], search2_fn=_boom)
    out = _text(await tool.token_resolve(ResolveParams(symbol="X")))
    assert OLD in out


@pytest.mark.asyncio
async def test_a_failing_first_index_does_not_lose_the_second():
    def _boom(_):
        raise RuntimeError("503")

    tool = DefiDataTool(search_fn=_boom, search2_fn=lambda s: [_cand(FRESH, 5.0)])
    out = _text(await tool.token_resolve(ResolveParams(symbol="X")))
    assert FRESH in out


@pytest.mark.asyncio
async def test_both_indexes_failing_is_an_error_not_an_empty_list():
    def _boom(_):
        raise RuntimeError("down")

    res = await DefiDataTool(search_fn=_boom, search2_fn=_boom).token_resolve(
        ResolveParams(symbol="X"))
    assert res.error


@pytest.mark.asyncio
async def test_empty_from_both_says_both_were_asked():
    tool = DefiDataTool(search_fn=lambda s: [], search2_fn=lambda s: [])
    out = _text(await tool.token_resolve(ResolveParams(symbol="NOPE")))
    assert "dexscreener" in out.lower() and "geckoterminal" in out.lower()
