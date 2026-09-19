"""Pool discovery as a first-class verb (proposal 029 R4).

`defi_data` could answer about a token you already knew; it had no verb for
"what launched in the last hour". So the prod agent scraped DexScreener and
GeckoTerminal through `web_fetch` every run, parsed JSON in-context, and filed
403s and "page params 400" in its reports. These tests pin the honesty
properties that a hand-rolled scrape kept losing.
"""
import pytest

from tools.defi.data_tool import DefiDataTool, DiscoverParams
from tools.defi.providers import geckoterminal as gt

TOK = "0xB20000000000000000000005b21D8272a739Ea01"


def _payload(reserve="12345.6", vol="2500.0", token_prefix="base",
             addr=None, dex="aerodrome-base"):
    return {"data": [{
        "attributes": {
            "address": "0xpool", "name": "MEME / WETH",
            "pool_created_at": "2026-08-24T16:08:05Z",
            "reserve_in_usd": reserve,
            "volume_usd": {"h24": vol},
            "price_change_percentage": {"h24": "42.0"},
        },
        "relationships": {
            "base_token": {"data": {"id": f"{token_prefix}_{addr or TOK}"}},
            "dex": {"data": {"id": dex}},
        },
    }]}


def _text(res):
    return res.extracted_content or ""


# --------------------------------------------------------------------------
# the parser — a weak claim must stay a weak claim
# --------------------------------------------------------------------------

def test_a_negative_reserve_is_unknown_not_zero():
    """9 of 20 rows in one live page reported a NEGATIVE reserve. '$0 liquidity'
    and 'the indexer has not caught up' are different facts, and only one of
    them is a reason to reject a token."""
    pools = gt.parse_pools(_payload(reserve="-3399.81"), "base", "base")
    assert pools[0].liquidity_usd is None


def test_a_zero_reserve_is_also_unknown():
    pools = gt.parse_pools(_payload(reserve="0"), "base", "base")
    assert pools[0].liquidity_usd is None


def test_a_real_reserve_survives():
    pools = gt.parse_pools(_payload(reserve="12345.6"), "base", "base")
    assert pools[0].liquidity_usd == 12345.6


def test_a_base_token_from_another_network_is_dropped():
    """The id is <network>_<address>. A row for another chain reported as if it
    were on this one is the same-address-different-token trap."""
    pools = gt.parse_pools(_payload(token_prefix="eth"), "base", "base")
    assert pools[0].base_token is None
    assert pools, "the row is still counted — hiding it would misreport the scan"


def test_a_malformed_address_is_dropped_not_guessed():
    pools = gt.parse_pools(_payload(addr="0xnothex"), "base", "base")
    assert pools[0].base_token is None


def test_the_dex_is_reported_verbatim():
    """The agent must be able to SEE that a pool is on v2/v4/aerodrome — that is
    exactly the fact the V3-only rail used to hide from it."""
    pools = gt.parse_pools(_payload(dex="uniswap-v4-base"), "base", "base")
    assert pools[0].dex == "uniswap-v4-base"


def test_a_junk_payload_yields_nothing_rather_than_raising():
    assert gt.parse_pools(None, "base", "base") == []
    assert gt.parse_pools({"data": "not-a-list"}, "base", "base") == []
    assert gt.parse_pools({"data": [None, 7]}, "base", "base") == []


def test_a_chain_the_indexer_does_not_cover_returns_nothing(monkeypatch):
    """Never another chain's pools. 'We could not look' is the honest answer.

    Proved against a row stripped of its id rather than a named chain: every
    chain in the registry is covered now (Robinhood gained coverage 2026-08-25),
    and a test that depends on which one is uncovered rots the moment that
    changes."""
    import dataclasses
    from core.wallet import chains
    bare = dataclasses.replace(chains.get("base"), geckoterminal_id=None)
    monkeypatch.setattr(chains, "get", lambda name: bare if name == "base" else None)
    assert gt.new_pools("base", fetch=lambda url: _payload()) == []


def test_an_unknown_chain_returns_nothing():
    assert gt.new_pools("nosuchchain", fetch=lambda url: _payload()) == []


def test_an_outage_is_empty_not_an_exception():
    def _boom(url):
        raise RuntimeError("503")
    assert gt.new_pools("base", fetch=_boom) == []


# --------------------------------------------------------------------------
# the verbs
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_pools_lists_what_it_found():
    tool = DefiDataTool(discover_fn=lambda chain, kind: gt.parse_pools(
        _payload(), "base", "base"))
    out = _text(await tool.new_pools(DiscoverParams()))
    assert "MEME / WETH" in out
    assert TOK in out
    assert "aerodrome-base" in out


@pytest.mark.asyncio
async def test_discovery_says_out_loud_that_nothing_is_screened():
    """The one thing this verb must never imply. It returns places to look."""
    tool = DefiDataTool(discover_fn=lambda chain, kind: gt.parse_pools(
        _payload(), "base", "base"))
    out = _text(await tool.new_pools(DiscoverParams())).lower()
    assert "not screened" in out or "screen" in out
    assert "token_info" in out, "name the verb that DOES screen"


@pytest.mark.asyncio
async def test_unknown_liquidity_renders_as_unknown_not_a_dollar_zero():
    tool = DefiDataTool(discover_fn=lambda chain, kind: gt.parse_pools(
        _payload(reserve="-1"), "base", "base"))
    out = _text(await tool.new_pools(DiscoverParams()))
    assert "unknown" in out.lower()
    assert "$0.00" not in out


@pytest.mark.asyncio
async def test_floors_exclude_a_thin_pool_but_say_how_many_were_excluded():
    """A silent filter reads as 'this is all there was'."""
    tool = DefiDataTool(discover_fn=lambda chain, kind: gt.parse_pools(
        _payload(reserve="500.0"), "base", "base"))
    out = _text(await tool.new_pools(DiscoverParams(min_liquidity_usd=3000.0)))
    assert "1" in out and "excluded" in out.lower()


@pytest.mark.asyncio
async def test_an_unknown_liquidity_row_is_not_silently_dropped_by_a_floor():
    """Unknown is not 'below the floor' — the agent decides, having been told."""
    tool = DefiDataTool(discover_fn=lambda chain, kind: gt.parse_pools(
        _payload(reserve="-1"), "base", "base"))
    out = _text(await tool.new_pools(DiscoverParams(min_liquidity_usd=3000.0)))
    assert "MEME / WETH" in out


@pytest.mark.asyncio
async def test_trending_is_a_separate_verb_with_its_own_caveat():
    tool = DefiDataTool(discover_fn=lambda chain, kind: gt.parse_pools(
        _payload(), "base", "base"))
    out = _text(await tool.trending(DiscoverParams()))
    assert "MEME / WETH" in out
    assert "purchasab" in out.lower() or "paid" in out.lower()


@pytest.mark.asyncio
async def test_an_empty_result_is_honest_about_being_empty():
    tool = DefiDataTool(discover_fn=lambda chain, kind: [])
    out = _text(await tool.new_pools(DiscoverParams()))
    assert "no pools" in out.lower()


@pytest.mark.asyncio
async def test_discovery_refuses_an_unsupported_chain():
    tool = DefiDataTool(discover_fn=lambda chain, kind: [])
    res = await tool.new_pools(DiscoverParams(chain="dogecoin"))
    assert res.error and "dogecoin" in res.error


# --------------------------------------------------------------------------
# swap_quote must ask the SAME router set that swap will (029 R1 follow-up)
#
# swap_quote called providers/univ3 directly while defi_trade.swap moved to the
# provider seam. That split is worse than either half alone: the READ verb would
# report "no route" for exactly the Aerodrome/V2 tokens the WRITE verb can now
# reach, so the agent would rule out a trade it was able to make - and it makes
# that call before ever loading the money tool.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_swap_quote_asks_the_route_seam_not_uniswap_directly():
    from tools.defi.data_tool import SwapQuoteParams
    from tools.defi.providers.routes import RouteQuote
    import time as _t

    seen = {}

    def _route(chain, ti, to_, amt, *, holder, slippage_bps):
        seen["called"] = True
        return RouteQuote(chain=chain, token_in=ti, token_out=to_,
                          amount_in_raw=amt, amount_out_raw=5 * 10 ** 17,
                          amount_out_min_raw=49 * 10 ** 16, spender="0xspend",
                          to="0xspend", calldata="0xabcd", value_raw=0,
                          venue="lifi:kyberswap", quoted_at=_t.time())

    tool = DefiDataTool(
        route_fn=_route,
        identity_fn=lambda c, a: type("I", (), {"symbol": "T", "name": "T",
                                                "decimals": 18, "verified": False,
                                                "metadata_changed": False})())
    out = _text(await tool.swap_quote(SwapQuoteParams(
        token_in="0x" + "11" * 20, token_out="0x" + "22" * 20, amount_in=1.0)))
    assert seen.get("called"), "the read verb must use the same seam as the write verb"
    assert "lifi:kyberswap" in out, "name the venue, so the agent knows which rail"


@pytest.mark.asyncio
async def test_swap_quote_no_route_names_who_was_asked():
    from tools.defi.data_tool import SwapQuoteParams
    from tools.defi.providers import routes
    tool = DefiDataTool(
        route_fn=lambda c, ti, to_, amt, *, holder, slippage_bps: (
            None, routes.no_route_reason(c, asked=["univ3", "lifi"])),
        identity_fn=lambda c, a: type("I", (), {"symbol": "T", "name": "T",
                                                "decimals": 18, "verified": False,
                                                "metadata_changed": False})())
    out = _text(await tool.swap_quote(SwapQuoteParams(
        token_in="0x" + "11" * 20, token_out="0x" + "22" * 20, amount_in=1.0)))
    assert "univ3" in out
    assert "unknown" in out.lower(), "no route is UNKNOWN, not a zero-value trade"


@pytest.mark.asyncio
async def test_swap_quote_never_asks_a_route_for_the_zero_address():
    """Caught live: LI.FI rejects the zero address outright ('/fromAddress Zero
    address is provided', HTTP 400), so a zero-address stand-in made the READ
    verb blind to exactly the aggregator routes it exists to surface - while the
    WRITE verb, which quotes for the real signer, could reach them."""
    from tools.defi.data_tool import SwapQuoteParams
    holders = []

    def _route(chain, ti, to_, amt, *, holder, slippage_bps):
        holders.append(holder)
        return None

    tool = DefiDataTool(
        route_fn=_route, holder=None,
        identity_fn=lambda c, a: type("I", (), {"symbol": "T", "name": "T",
                                                "decimals": 18, "verified": False,
                                                "metadata_changed": False})())
    await tool.swap_quote(SwapQuoteParams(
        token_in="0x" + "11" * 20, token_out="0x" + "22" * 20, amount_in=1.0))
    assert holders
    assert int(holders[0], 16) != 0, "a zero-address quote is refused by providers"


@pytest.mark.asyncio
async def test_swap_quote_prefers_the_wallets_own_address_when_there_is_one():
    """A route can depend on the address (balances, allowances), so quoting for
    the signer that would actually execute is the closest read to the truth."""
    from tools.defi.data_tool import SwapQuoteParams
    holders = []

    def _route(chain, ti, to_, amt, *, holder, slippage_bps):
        holders.append(holder)
        return None

    mine = "0x" + "77" * 20
    tool = DefiDataTool(
        route_fn=_route, holder=mine,
        identity_fn=lambda c, a: type("I", (), {"symbol": "T", "name": "T",
                                                "decimals": 18, "verified": False,
                                                "metadata_changed": False})())
    await tool.swap_quote(SwapQuoteParams(
        token_in="0x" + "11" * 20, token_out="0x" + "22" * 20, amount_in=1.0))
    assert holders == [mine]


# --------------------------------------------------------------------------
# D4 — the discovery render carries the separators, not just the headline
# --------------------------------------------------------------------------

def _rich_pool(**over):
    d = dict(chain="base", pool_address="0xpool", base_token=TOK,
             name="MEME / WETH", dex="aerodrome-base",
             created_at="2026-08-24T16:08:05Z",
             liquidity_usd=250_000.0, volume_h24_usd=3_000_000.0,
             price_change_h24_pct=42.0, volume_h1_usd=200_000.0,
             market_cap_usd=2_500_000.0, fdv_usd=2_600_000.0,
             trades_h24=gt.PoolTrades(buys=14641, sells=3768, buyers=570, sellers=400),
             trades_h1=gt.PoolTrades(buys=100, sells=90, buyers=40, sellers=35))
    d.update(over)
    return gt.PoolCandidate(**d)


@pytest.mark.asyncio
async def test_discovery_states_the_volume_over_liquidity_ratio():
    tool = DefiDataTool(discover_fn=lambda c, k: [_rich_pool()])
    out = _text(await tool.new_pools(DiscoverParams(chain="base", min_liquidity_usd=0)))
    assert "V/L" in out and "12" in out


@pytest.mark.asyncio
async def test_discovery_states_unique_buyers_and_txns_per_buyer():
    tool = DefiDataTool(discover_fn=lambda c, k: [_rich_pool()])
    out = _text(await tool.new_pools(DiscoverParams(chain="base", min_liquidity_usd=0)))
    assert "570" in out            # unique buyers
    assert "buyers" in out.lower()


@pytest.mark.asyncio
async def test_discovery_states_market_cap():
    tool = DefiDataTool(discover_fn=lambda c, k: [_rich_pool()])
    out = _text(await tool.new_pools(DiscoverParams(chain="base", min_liquidity_usd=0)))
    assert "mcap" in out.lower() or "market cap" in out.lower()


@pytest.mark.asyncio
async def test_a_missing_ratio_renders_unknown_never_zero():
    tool = DefiDataTool(discover_fn=lambda c, k: [
        _rich_pool(liquidity_usd=None, trades_h24=None, market_cap_usd=None)])
    out = _text(await tool.new_pools(DiscoverParams(chain="base", min_liquidity_usd=0)))
    assert "unknown" in out.lower()
    assert "V/L 0" not in out


@pytest.mark.asyncio
async def test_swap_quote_is_stamped_so_two_reads_are_never_byte_identical():
    """2026-09-19 (goal aaaa0bf1bdda): the history compaction pass replaces a
    byte-identical tool output with a back-reference. Two quotes minutes apart
    that happen to agree collapsed into "[duplicate of an earlier tool result]",
    so the agent could not tell a fresh re-quote from a suppressed call. A quote
    is a time-stamped observation: carry `quoted_at` (UTC) and the token
    addresses in the text so no two reads are identical bytes."""
    from tools.defi.data_tool import SwapQuoteParams
    from tools.defi.providers.routes import RouteQuote

    stamps = iter([1789800000.0, 1789800061.0])

    def _route(chain, ti, to_, amt, *, holder, slippage_bps):
        return RouteQuote(chain=chain, token_in=ti, token_out=to_,
                          amount_in_raw=amt, amount_out_raw=5 * 10 ** 17,
                          amount_out_min_raw=49 * 10 ** 16, spender="0xspend",
                          to="0xspend", calldata="0xabcd", value_raw=0,
                          venue="lifi:kyberswap", quoted_at=next(stamps))

    tool = DefiDataTool(
        route_fn=_route,
        identity_fn=lambda c, a: type("I", (), {"symbol": "T", "name": "T",
                                                "decimals": 18, "verified": False,
                                                "metadata_changed": False})())
    p = SwapQuoteParams(token_in="0x" + "11" * 20, token_out="0x" + "22" * 20, amount_in=1.0)
    a = _text(await tool.swap_quote(p))
    b = _text(await tool.swap_quote(p))
    assert "quoted_at:" in a and "Z" in a
    assert "0x" + "11" * 20 in a.lower() or "0x1111" in a.lower()
    assert a != b, "same rate a minute later must still be a distinct observation"
