"""`swap_quote` cross-checks the route's answer against the indexer-implied
output and says SUSPECT when they disagree by more than 5×.

Prod 2026-09-19 11:4x: the `lifi:fly` route returned ~4.1 WETH for four
different small tokens on a cold first response (a route-level constant, not
a units error — the probe goal did not reproduce it). A quote is INPUT to the
swap rail's min-out floor, and the EXIT rail spent two extra steps re-quoting
"implausible" numbers by eye. The cross-check is keyless (the same indexer
price `defi_data.price` reads), fail-open (no price ⇒ "unavailable", never a
verdict), and gated on real liquidity (a liquidity:0 pool once priced a token
at 1e35).
"""
import time as _t

import pytest

from tools.defi.data_tool import DefiDataTool, SwapQuoteParams, quote_cross_check
from tools.defi.providers.base import PriceInfo
from tools.defi.providers.routes import RouteQuote

IN = "0x" + "11" * 20
OUT = "0x" + "22" * 20


def _text(res):
    return res.extracted_content or res.error or ""


def _identity(c, a):
    return type("I", (), {"symbol": "T", "name": "T", "decimals": 18,
                          "verified": False, "metadata_changed": False})()


def _tool(out_raw, prices):
    def _route(chain, ti, to_, amt, *, holder, slippage_bps):
        return RouteQuote(chain=chain, token_in=ti, token_out=to_, amount_in_raw=amt,
                          amount_out_raw=out_raw, amount_out_min_raw=out_raw,
                          spender="0xspend", to="0xspend", calldata="0x", value_raw=0,
                          venue="lifi:fly", quoted_at=_t.time())
    return DefiDataTool(route_fn=_route, identity_fn=_identity,
                        price_fn=lambda c, a: prices[a.lower()])


def _p(usd, liq=1_000_000.0):
    return PriceInfo(price_usd=usd, liquidity_usd=liq, pool_count=1,
                     confidence="high" if usd is not None else "unknown")


# ---- pure helper -----------------------------------------------------------

def test_cross_check_pure_ratio_and_verdicts():
    # implied out = amount_in * p_in / p_out = 1 * 2 / 1 = 2
    ok = quote_cross_check(1.0, 2.1, _p(2.0), _p(1.0))
    assert ok.verdict == "consistent" and abs(ok.ratio - 1.05) < 1e-9
    high = quote_cross_check(1.0, 40.0, _p(2.0), _p(1.0))
    assert high.verdict == "suspect" and high.ratio == 20.0
    low = quote_cross_check(1.0, 0.1, _p(2.0), _p(1.0))
    assert low.verdict == "suspect" and low.ratio == 0.05


@pytest.mark.parametrize("p_in,p_out", [
    (_p(None), _p(1.0)),          # no price for token_in
    (_p(2.0), _p(None)),          # no price for token_out
    (_p(2.0, liq=0.0), _p(1.0)),  # zero-liquidity pool priced token_in (the 1e35 case)
    (_p(2.0), _p(1.0, liq=None)),  # liquidity unknown
    (_p(0.0), _p(1.0)),           # zero price is unknown, not free
])
def test_cross_check_unavailable_never_a_verdict(p_in, p_out):
    r = quote_cross_check(1.0, 40.0, p_in, p_out)
    assert r.verdict == "unavailable" and r.ratio is None


# ---- rendered through the verb ---------------------------------------------

@pytest.mark.asyncio
async def test_swap_quote_flags_a_20x_quote_as_suspect():
    tool = _tool(40 * 10 ** 18, {IN: _p(2.0), OUT: _p(1.0)})
    out = _text(await tool.swap_quote(SwapQuoteParams(token_in=IN, token_out=OUT, amount_in=1.0)))
    assert "SUSPECT" in out
    assert "20.0x" in out and "implied" in out.lower()
    assert "min-out" in out or "floor" in out, "must say not to use it as a floor"


@pytest.mark.asyncio
async def test_swap_quote_consistent_quote_says_so():
    tool = _tool(21 * 10 ** 17, {IN: _p(2.0), OUT: _p(1.0)})
    out = _text(await tool.swap_quote(SwapQuoteParams(token_in=IN, token_out=OUT, amount_in=1.0)))
    assert "SUSPECT" not in out
    assert "cross-check: consistent" in out


@pytest.mark.asyncio
async def test_swap_quote_without_indexer_price_says_unavailable_not_suspect():
    tool = _tool(40 * 10 ** 18, {IN: _p(2.0), OUT: _p(None)})
    out = _text(await tool.swap_quote(SwapQuoteParams(token_in=IN, token_out=OUT, amount_in=1.0)))
    assert "SUSPECT" not in out
    assert "cross-check: unavailable" in out


@pytest.mark.asyncio
async def test_swap_quote_price_lookup_failure_is_fail_open():
    def _boom(c, a):
        raise RuntimeError("indexer down")
    def _route(chain, ti, to_, amt, *, holder, slippage_bps):
        return RouteQuote(chain=chain, token_in=ti, token_out=to_, amount_in_raw=amt,
                          amount_out_raw=10 ** 18, amount_out_min_raw=10 ** 18,
                          spender="0xspend", to="0xspend", calldata="0x", value_raw=0,
                          venue="lifi:fly", quoted_at=_t.time())
    tool = DefiDataTool(route_fn=_route, identity_fn=_identity, price_fn=_boom)
    out = _text(await tool.swap_quote(SwapQuoteParams(token_in=IN, token_out=OUT, amount_in=1.0)))
    assert "lifi:fly" in out and "cross-check: unavailable" in out


# ---- 'native' is a first-class token_in/out, as it is on the swap verb -------

WETH_BASE = "0x4200000000000000000000000000000000000006"


@pytest.mark.asyncio
async def test_swap_quote_accepts_the_literal_native_and_says_what_it_quoted_via():
    """2026-09-21 (intel): the swap verb PREFERS token_in='native' when the wallet
    holds the gas asset, but swap_quote ran `_validate` on the literal and
    refused it ("not a 20-byte hex address: 'native'") — 13 refused quotes in
    three days, one wasted step per money-rail run before the agent re-quoted
    via the wrapped-native address. The quote verb now resolves 'native' to the
    chain registry's wrapped_native row (the asset the EVM routers price) and
    names it in the result."""
    calls = {}

    def _route(chain, ti, to_, amt, *, holder, slippage_bps):
        calls["ti"] = ti
        return RouteQuote(chain=chain, token_in=ti, token_out=to_, amount_in_raw=amt,
                          amount_out_raw=21 * 10 ** 17, amount_out_min_raw=21 * 10 ** 17,
                          spender="0xspend", to="0xspend", calldata="0x", value_raw=0,
                          venue="lifi:fly", quoted_at=_t.time())
    tool = DefiDataTool(route_fn=_route, identity_fn=_identity,
                        price_fn=lambda c, a: {WETH_BASE.lower(): _p(2.0), OUT.lower(): _p(1.0)}[a.lower()])
    res = await tool.swap_quote(SwapQuoteParams(token_in="native", token_out=OUT, amount_in=1.0))
    assert res.error is None, res.error
    assert calls["ti"].lower() == WETH_BASE.lower()
    assert "native" in _text(res).lower() and WETH_BASE[:8].lower() in _text(res).lower()


# ---- USD value of the output (2026-09-22) ---------------------------------
# The EXIT rail quotes every open row into WETH and then converts by hand
# ("WETH≈$2,700 est; I need the ETH price to convert" — 02:01Z run). The
# cross-check already reads the indexed price of the OUT side; render the
# output's dollar value from that same read, and say UNKNOWN when it is not
# usable — never a guessed rate.

@pytest.mark.asyncio
async def test_swap_quote_renders_the_output_value_in_usd():
    # 2.1 OUT at $1.00 each; IN at $2.00 (consistent quote)
    tool = _tool(21 * 10 ** 17, {IN: _p(2.0), OUT: _p(1.0)})
    out = _text(await tool.swap_quote(SwapQuoteParams(token_in=IN, token_out=OUT, amount_in=1.0)))
    assert "value:  ≈ $2.10" in out
    assert "$1.00 per T" in out  # names the rate it used, and that it is indexed


@pytest.mark.asyncio
async def test_swap_quote_value_is_unknown_without_a_usable_out_price():
    tool = _tool(21 * 10 ** 17, {IN: _p(2.0), OUT: _p(None)})
    out = _text(await tool.swap_quote(SwapQuoteParams(token_in=IN, token_out=OUT, amount_in=1.0)))
    assert "value:  unknown" in out
    assert "$0.00" not in out
