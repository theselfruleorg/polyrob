"""Spending the NATIVE gas asset directly into a position.

Live, 2026-09-13. `core/wallet/chains.py` has recorded since 2026-09-10, verified
against the live aggregator, that on Robinhood "native-ETH->meme quotes" and
"Native ETH in is the cheapest entry and needs no allowance at all". The rail
could not express it:

  * `SwapParams.token_in` demanded a CONTRACT ADDRESS and there was no native
    sentinel anywhere in the tree;
  * `routes/lifi.py` and `routes/best_route` BOTH refused any quote carrying
    native value, on the reasoning that it was "an unasserted native outflow
    riding along with the trade".

That reasoning was true when it was written and stale by 039 B1, which taught
`tx_guard` to DECLARE and assert a native send (`TxIntent.token=None` measures
the native delta and refuses a short or long move). So the wallet could only
enter through WETH -- which meant an allowance to grant, gas to pay for it, and
WETH it did not hold. The agent spent twelve hours trying to bridge $3 to obtain
WETH while holding 0.0512 native ETH on the very chain it wanted to trade on.

What these tests pin is the replacement assertion: not "no value" but "EXACTLY
the value we asked to sell", plus the declaration that makes the guard measure it.
"""
import time

import pytest

from tools.defi.providers.routes import (NATIVE, RouteQuote, best_route_with_reason,
                                         is_native)

CHAIN = "base"
TOKEN_OUT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Base USDC
AMOUNT = 10 ** 15


def _quote(*, value_raw, token_in=NATIVE, amount_in_raw=AMOUNT):
    from core.wallet import chains
    spender = chains.get(CHAIN).aggregator_spender
    return RouteQuote(
        chain=CHAIN, token_in=token_in, token_out=TOKEN_OUT,
        amount_in_raw=amount_in_raw, amount_out_raw=2_500_000,
        # Must CLEAR the caller's own 150bps floor (2_500_000 * 9850 / 10000 =
        # 2_462_500). `best_route` re-derives it and takes the lower of the two,
        # so a provider can tighten the floor but never loosen it.
        amount_out_min_raw=2_462_500, spender=spender, to=spender,
        calldata="0x" + "11" * 40, value_raw=value_raw,
        venue="lifi:test", quoted_at=time.time(), locally_built=False)


class _Prov:
    name = "stub"

    def __init__(self, quote):
        self._q = quote

    def supports(self, chain):
        return True

    def quote(self, chain, token_in, token_out, amount_in_raw, *, holder,
              slippage_bps):
        return self._q


def _route(quote, token_in=NATIVE):
    return best_route_with_reason(
        CHAIN, token_in, TOKEN_OUT, AMOUNT,
        holder="0x2222222222222222222222222222222222222222",
        slippage_bps=150, providers=(_Prov(quote),))


# --- the sentinel ----------------------------------------------------------

def test_the_sentinel_is_a_word_not_an_address():
    """Every 0x value on this rail is checksum-validated and pinned against the
    chain registry. A magic zero-address sentinel would travel through all of
    that machinery as if it were a real contract, and the one thing that must
    never happen on a money path is an address that means something else."""
    assert NATIVE == "native"
    assert not NATIVE.startswith("0x")
    assert is_native("native") and is_native("NATIVE") and is_native(" Native ")
    assert not is_native(TOKEN_OUT) and not is_native(None) and not is_native("")


# --- best_route: the replacement assertion ---------------------------------

def test_a_native_swap_carrying_exactly_the_priced_amount_is_ACCEPTED():
    route, why = _route(_quote(value_raw=AMOUNT))
    assert route is not None, why
    assert route.value_raw == AMOUNT


@pytest.mark.parametrize("value,label", [
    (AMOUNT + 1, "one wei over"),
    (AMOUNT - 1, "one wei under"),
    (AMOUNT * 2, "double"),
    (0, "none at all"),
])
def test_a_native_swap_carrying_any_OTHER_amount_is_REFUSED(value, label):
    """The bound is exact, in both directions. A quote that moves more native
    than was priced is a bigger trade than the one authorized; one that moves
    less is not the trade that was quoted. `0` is included deliberately -- a
    native swap that pushes nothing cannot be the transaction we asked for."""
    route, why = _route(_quote(value_raw=value))
    assert route is None, f"{label} was accepted: {route}"


def test_an_ERC20_swap_still_refuses_ANY_native_value():
    """The original guarantee is untouched for the non-native path: there, value
    really is an undeclared outflow, because the intent declares an ERC-20."""
    route, why = _route(_quote(value_raw=1, token_in=TOKEN_OUT),
                        token_in=TOKEN_OUT)
    assert route is None


# --- the univ3 provider ----------------------------------------------------

def test_univ3_declines_native_instead_of_treating_it_as_an_address():
    """V3 pools hold the WRAPPED asset and `exactInputSingle` pulls by
    allowance, so there is no native path to build locally. It must return None
    -- "this provider has no route" -- and NOT pass the word to an address
    encoder, and NOT be read as "unbuyable"."""
    from tools.defi.providers.routes.univ3_route import UniV3RouteProvider
    p = UniV3RouteProvider()
    assert p.quote(CHAIN, NATIVE, TOKEN_OUT, AMOUNT,
                   holder="0x2222222222222222222222222222222222222222",
                   slippage_bps=150) is None


# --- the lifi provider -----------------------------------------------------

def test_lifi_translates_the_sentinel_at_the_edge_and_keeps_the_value():
    """Measured against the live API on Robinhood 2026-09-13: a native quote
    returns value == fromAmount, an ERC-20 quote returns 0x0."""
    from core.wallet import chains
    from tools.defi.providers.routes.lifi import LifiRouteProvider

    seen = {}

    def _fetch(url):
        seen["url"] = url
        return {
            "estimate": {"toAmount": "2500000",
                         "approvalAddress": chains.get(CHAIN).aggregator_spender},
            "transactionRequest": {
                "to": chains.get(CHAIN).aggregator_spender,
                "data": "0x" + "22" * 40,
                "value": hex(AMOUNT),
                "chainId": chains.get(CHAIN).chain_id},
            "tool": "test"}

    q = LifiRouteProvider(fetch=_fetch).quote(
        CHAIN, NATIVE, TOKEN_OUT, AMOUNT,
        holder="0x2222222222222222222222222222222222222222", slippage_bps=150)
    assert q is not None
    assert q.value_raw == AMOUNT, "the native amount must survive to best_route"
    assert "fromToken=0x0000000000000000000000000000000000000000" in seen["url"]
    assert "native" not in seen["url"], "our sentinel must not reach the API"


# --- the verb --------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_verb_declares_a_NATIVE_send_and_skips_the_allowance():
    """Two things must be true at once, and each is load-bearing.

    `TxIntent.token=None` is the DECLARATION that makes tx_guard measure the
    native delta (039 B1). Declaring the sentinel -- or any address -- instead
    would send the guard hunting for an ERC-20 outflow that does not exist, and
    its `intent.token` branch additionally REFUSES an unexpected native change,
    so the trade would be refused by the guard meant to bound it.

    And no allowance is read: native value is PUSHED with the call, never pulled
    by a spender, so `read_allowance` would be asking an ERC-20 question about an
    asset with no contract. That is the registry's "needs no allowance at all",
    and it removes an approve transaction, its gas and its tap from every entry.
    """
    import contextlib

    from core.wallet import chains
    from core.wallet.tx_guard import Decision
    from tools.defi.trade_tool import DefiTradeTool, SwapParams

    spender = chains.get(CHAIN).aggregator_spender
    captured, allowance_reads = [], []

    class _Gate:
        @contextlib.asynccontextmanager
        async def reserve(self):
            yield

        def record(self, **kw):
            pass

    class _Signer:
        address = "0x2222222222222222222222222222222222222222"

    class _Wallet:
        policy = _Gate()

        def operational_signer(self):
            return _Signer()

    class _Rail:
        def __init__(self, chain, signer, **kw):
            pass

        def build_call(self, *, to, data, value=0):
            return {"to": to, "data": data, "value": value}

    def _guard(intent, tx, **kw):
        captured.append((intent, tx))
        return Decision(allowed=False, reason="stop here", lane="autonomous",
                        amount_usd=3.0)

    # Per-token prices, so the route-sanity check AGREES. A flat scalar prices
    # USDC at $2,500 and the swap is (correctly) refused for 99.96% drift --
    # which is also a live demonstration that the native side is priced through
    # the chain's pinned wrapped native, exactly as tx_guard prices it.
    wrapped = chains.get(CHAIN).wrapped_native
    _prices = {wrapped.lower(): 2500.0, TOKEN_OUT.lower(): 1.0}

    import tools.defi.providers.univ3 as _univ3
    real_allow = _univ3.read_allowance
    _univ3.read_allowance = lambda *a, **k: allowance_reads.append(a) or 0
    try:
        tool = DefiTradeTool(
            wallet=_Wallet(), rail_factory=_Rail, guard_fn=_guard,
            price_fn=lambda c, a: _prices.get(str(a).lower()),
            fallback_price_fn=lambda c, a: None,
            balance_fn=lambda c, h, t: None,
            route_fn=lambda *a, **k: (_quote(value_raw=AMOUNT), "ok"))
        await tool.swap(SwapParams(
            chain=CHAIN, token_in=NATIVE, token_out=TOKEN_OUT,
            amount_in=0.001, max_spend_usd=5.0, dry_run=True))
    finally:
        _univ3.read_allowance = real_allow

    assert captured, "the guard was never consulted"
    intent, tx = captured[0]
    assert intent.token is None, "a native send must be DECLARED, not implied"
    assert intent.inflow_token == TOKEN_OUT, "the bought token is still asserted"
    assert intent.amount_raw == AMOUNT
    assert tx["value"] == AMOUNT, "the native value must reach the transaction"
    assert not allowance_reads, "a native swap must not read an allowance"
