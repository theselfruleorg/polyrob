"""The route-provider seam (proposal 029 R1).

The rail spoke Uniswap V3 only, so every fresh Base launch that pooled on
Aerodrome or a V2 fork was unreachable — the prod agent filed that as its #1
standing owner ask on 15+ consecutive runs. These tests pin the properties that
make a THIRD-PARTY route admissible at all: the spender must be a pinned,
on-chain-verified address for THAT chain, and the output floor must be ours.
"""
import time

import pytest

from tools.defi.providers import routes

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_WETH = "0x4200000000000000000000000000000000000006"
LIFI_DIAMOND = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
ROGUE = "0xBAdBadbADBaDBADBadbADbaDBadBaDBADBadBAD0"
_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"   # Base SwapRouter02, pinned


def _quote(**kw):
    base = dict(chain="base", token_in=BASE_USDC, token_out=BASE_WETH,
                amount_in_raw=2_000_000, amount_out_raw=1_000_000_000_000_000,
                amount_out_min_raw=1_000_000_000_000_000, spender=LIFI_DIAMOND,
                to=LIFI_DIAMOND, calldata="0xdeadbeef", value_raw=0,
                venue="lifi:kyberswap", quoted_at=time.time(),
                locally_built=False)
    base.update(kw)
    return routes.RouteQuote(**base)


class _Stub:
    """A route provider that returns whatever the test hands it."""

    def __init__(self, name, quote, chains=("base",)):
        self.name = name
        self._quote = quote
        self._chains = chains
        self.calls = 0

    def supports(self, chain):
        return chain in self._chains

    def quote(self, chain, token_in, token_out, amount_in_raw, *, holder, slippage_bps):
        self.calls += 1
        return self._quote


# --------------------------------------------------------------------------
# ordering — local construction is tried first, always
# --------------------------------------------------------------------------

def test_the_first_provider_that_routes_wins_and_the_rest_are_not_asked():
    first = _Stub("univ3", _quote(venue="uniswap-v3", spender=_ROUTER,
                                  to=_ROUTER, locally_built=True))
    second = _Stub("lifi", _quote())
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(first, second))
    assert got.venue == "uniswap-v3"
    assert second.calls == 0, "a third party must never be asked when we can build locally"


def test_a_none_from_the_first_provider_falls_through_to_the_next():
    """This is the entire point of 029: today a None from univ3 is the end."""
    first = _Stub("univ3", None)
    second = _Stub("lifi", _quote())
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(first, second))
    assert got is not None and got.venue == "lifi:kyberswap"


def test_no_route_anywhere_fails_open_to_none():
    """No route is UNKNOWN, never a zero-output trade."""
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("univ3", None), _Stub("lifi", None)))
    assert got is None


def test_a_provider_that_raises_does_not_take_the_others_down():
    class _Boom:
        name = "boom"
        def supports(self, chain): return True
        def quote(self, *a, **k): raise RuntimeError("api down")

    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Boom(), _Stub("lifi", _quote())))
    assert got is not None


def test_a_provider_that_does_not_support_the_chain_is_skipped():
    off = _Stub("univ3", _quote(), chains=("ethereum",))
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(off, _Stub("lifi", _quote())))
    assert off.calls == 0
    assert got.venue == "lifi:kyberswap"


# --------------------------------------------------------------------------
# the spender allowlist — the property V3 gave us for free
# --------------------------------------------------------------------------

def test_an_unpinned_spender_is_refused():
    """With V3 the spender came from the pinned ChainRow. A third party names
    its own, so it must be checked against the row or funds are approved to an
    address nobody verified."""
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(spender=ROGUE)),))
    assert got is None


def test_the_pinned_spender_is_accepted_case_insensitively():
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(spender=LIFI_DIAMOND.lower(),
                                                            to=LIFI_DIAMOND.lower())),))
    assert got is not None


def test_a_spender_pinned_on_another_chain_is_not_borrowed():
    """chains.py's core rule: a refusal never points at another chain's value."""
    got = routes.best_route("robinhood", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(chain="robinhood"),
                                             chains=("robinhood",)),))
    assert got is None, "robinhood has no verified aggregator spender"


def test_a_locally_built_route_needs_no_allowlist_entry():
    """univ3's spender IS the pinned router, checked by the registry already."""
    from core.wallet import chains
    router = chains.get("base").univ3_router
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("univ3", _quote(venue="uniswap-v3",
                                                             spender=router, to=router,
                                                             locally_built=True)),))
    assert got is not None


# --------------------------------------------------------------------------
# the output floor — ours, never only theirs
# --------------------------------------------------------------------------

def test_a_third_party_minimum_looser_than_our_slippage_is_refused():
    """We cannot rewrite their calldata, so a loose floor is a REFUSAL, not a
    number we can override — this is the asymmetry that matters."""
    loose = _quote(amount_out_raw=1_000_000, amount_out_min_raw=1)   # ~100% slippage
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", loose),))
    assert got is None


def test_a_third_party_minimum_that_clears_our_bound_is_kept_verbatim():
    ok = _quote(amount_out_raw=1_000_000, amount_out_min_raw=990_000)
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", ok),))
    assert got.amount_out_min_raw == 990_000, "their encoded floor is the real floor"


def test_a_one_unit_rounding_difference_does_not_refuse_a_good_route():
    """Measured against the live API: LI.FI's toAmountMin lands within +/-1 raw
    unit of ours. A strict comparison would refuse real routes on rounding."""
    ours = (1_000_000 * 9850) // 10_000
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(amount_out_raw=1_000_000,
                                                            amount_out_min_raw=ours - 1)),))
    assert got is not None


def test_a_third_party_route_with_no_minimum_at_all_is_refused():
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(amount_out_min_raw=None)),))
    assert got is None, "an unverifiable floor is not a floor"


def test_a_locally_built_route_gets_our_floor_because_we_encoded_it():
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("univ3", _quote(
                                venue="uniswap-v3", spender=_ROUTER, to=_ROUTER,
                                locally_built=True, amount_out_raw=1_000_000,
                                amount_out_min_raw=985_000)),))
    assert got.amount_out_min_raw == 985_000


def test_a_route_whose_floor_collapses_to_zero_is_refused():
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=10_000,
                            providers=(_Stub("lifi", _quote(amount_out_raw=100)),))
    assert got is None


def test_a_zero_output_quote_is_refused():
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(amount_out_raw=0)),))
    assert got is None


def test_empty_calldata_is_refused():
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(calldata="0x")),))
    assert got is None


# --------------------------------------------------------------------------
# which providers are live — the aggregator is OFF by default
# --------------------------------------------------------------------------

def test_the_aggregator_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DEFI_ROUTE_AGGREGATOR", raising=False)
    names = [p.name for p in routes.providers()]
    assert names == ["univ3"], "shipping the seam must change nothing until it is flipped"


def test_naming_the_aggregator_appends_it_after_local_construction(monkeypatch):
    monkeypatch.setenv("DEFI_ROUTE_AGGREGATOR", "lifi")
    names = [p.name for p in routes.providers()]
    assert names == ["univ3", "lifi"]


@pytest.mark.parametrize("value", ["", "none", "off", "false", "0", "  "])
def test_falsey_values_leave_the_aggregator_off(monkeypatch, value):
    monkeypatch.setenv("DEFI_ROUTE_AGGREGATOR", value)
    assert [p.name for p in routes.providers()] == ["univ3"]


def test_an_unknown_aggregator_name_is_refused_not_guessed(monkeypatch):
    monkeypatch.setenv("DEFI_ROUTE_AGGREGATOR", "totallyreal")
    assert [p.name for p in routes.providers()] == ["univ3"]


# --------------------------------------------------------------------------
# Hardening pass (2026-08-25): what a HOSTILE or BROKEN provider could send
#
# best_route pinned the SPENDER but not the call TARGET, and accepted both
# verbatim without checking they were even well-formed addresses. tx_guard would
# still have caught a bad outcome (it simulates and asserts deltas, and the
# allowance only ever reaches the pinned spender), but "the last gate catches it"
# is not a reason to hand an unvalidated address to the rail.
# --------------------------------------------------------------------------

def test_an_aggregator_call_target_that_is_not_the_pinned_spender_is_refused():
    """A quote naming the pinned spender but pointing the CALL somewhere else."""
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(to=ROGUE)),))
    assert got is None


def test_a_malformed_spender_is_refused_before_it_reaches_the_rail():
    for bad in ("", "0xnothex", "0x1234", "not-an-address", None):
        got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                                holder=ROGUE, slippage_bps=150,
                                providers=(_Stub("lifi", _quote(spender=bad, to=bad)),))
        assert got is None, bad


def test_a_malformed_call_target_is_refused():
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(to="0xnothex")),))
    assert got is None


def test_a_locally_built_route_may_call_its_own_router():
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("univ3", _quote(
                                venue="uniswap-v3", spender=_ROUTER, to=_ROUTER,
                                locally_built=True)),))
    assert got is not None


def test_calldata_that_is_not_hex_is_refused():
    for bad in ("0xzzzz", "deadbeef", "0x123"):
        got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                                holder=ROGUE, slippage_bps=150,
                                providers=(_Stub("lifi", _quote(calldata=bad)),))
        assert got is None, bad


def test_a_non_zero_native_value_on_an_erc20_swap_is_refused():
    """An unasserted native outflow riding along with the trade."""
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote(value_raw=10 ** 15)),))
    assert got is None


# --------------------------------------------------------------------------
# "no route" must not be said when we simply could not ask
# --------------------------------------------------------------------------

def test_a_provider_outage_is_reported_as_an_outage_not_as_no_route():
    """The agent writes reports concluding a token is unreachable. A throttled
    or down API must not produce that conclusion — it is a different fact and a
    different next move (retry), and it is the difference between 'this token
    cannot be bought' and 'we could not look right now'."""
    class _Down:
        name = "lifi"
        def supports(self, chain): return True
        def quote(self, *a, **k): raise RuntimeError("429 Too Many Requests")

    got, why = routes.best_route_with_reason(
        "base", BASE_USDC, BASE_WETH, 2_000_000, holder=ROGUE,
        slippage_bps=150, providers=(_Stub("univ3", None), _Down()))
    assert got is None
    assert "unavailable" in why.lower() or "could not" in why.lower()
    assert "lifi" in why


def test_a_genuine_no_route_says_so_plainly():
    got, why = routes.best_route_with_reason(
        "base", BASE_USDC, BASE_WETH, 2_000_000, holder=ROGUE,
        slippage_bps=150, providers=(_Stub("univ3", None), _Stub("lifi", None)))
    assert got is None
    assert "no route" in why.lower()
    assert "unavailable" not in why.lower()


def test_best_route_still_returns_just_the_quote():
    """The simple entry point keeps its shape; the reason is opt-in."""
    got = routes.best_route("base", BASE_USDC, BASE_WETH, 2_000_000,
                            holder=ROGUE, slippage_bps=150,
                            providers=(_Stub("lifi", _quote()),))
    assert isinstance(got, routes.RouteQuote)


# --------------------------------------------------------------------------
# LI.FI's token index is case-sensitive on some chains (2026-08-25)
#
# Measured on Robinhood Chain (4663): the SAME pair quotes fine with a
# lowercase token address and returns HTTP 404 "no available quotes" with the
# EIP-55 checksummed form. We checksum every EVM address on the way in (it is
# the only typo detector Ethereum has), so we were handing the API the one form
# it rejects — and a 404 fails open to "no route", which reads as "this token is
# unbuyable" for a token that routes fine.
#
# Lowercasing is safe for an EVM hex address in the OUTBOUND query only: it is
# the canonical unchecksummed form, we still hold the checksummed value, and the
# spender/target that comes BACK is still validated against the chain's pin.
# The Solana lesson does not apply here — that is base58, where case is data.
# --------------------------------------------------------------------------

def test_the_lifi_query_sends_lowercase_token_addresses():
    from tools.defi.providers.routes.lifi import LifiRouteProvider
    seen = {}

    def _fetch(url):
        seen["url"] = url
        raise RuntimeError("stop here — the URL is what is under test")

    LifiRouteProvider(fetch=_fetch).quote(
        "base", BASE_USDC, BASE_WETH, 2_000_000, holder=ROGUE, slippage_bps=150)
    url = seen["url"]
    assert BASE_USDC.lower() in url and BASE_USDC not in url
    assert BASE_WETH.lower() in url


def test_the_lifi_query_sends_a_lowercase_holder_too():
    from tools.defi.providers.routes.lifi import LifiRouteProvider
    seen = {}

    def _fetch(url):
        seen["url"] = url
        raise RuntimeError("stop")

    LifiRouteProvider(fetch=_fetch).quote(
        "base", BASE_USDC, BASE_WETH, 2_000_000, holder=ROGUE, slippage_bps=150)
    assert ROGUE.lower() in seen["url"]


def test_a_checksummed_spender_in_the_RESPONSE_is_still_accepted():
    """Lowercasing is outbound-only. What comes back is compared case-
    insensitively against the pin, which it already was."""
    from tools.defi.providers.routes.lifi import LifiRouteProvider
    body = {
        "tool": "kyberswap",
        "estimate": {"toAmount": "1000000", "toAmountMin": "990000",
                     "approvalAddress": LIFI_DIAMOND},
        "transactionRequest": {"to": LIFI_DIAMOND, "data": "0xdeadbeef",
                               "value": "0x0", "chainId": 8453},
    }
    q = LifiRouteProvider(fetch=lambda url: body).quote(
        "base", BASE_USDC, BASE_WETH, 2_000_000, holder=ROGUE, slippage_bps=150)
    assert q is not None and q.spender == LIFI_DIAMOND
