"""WAL-7: the on-chain minimum inside a LI.FI diamond swap is read from the
signed bytes and held to our floor; an unknown facet falls back to the
measured-inflow floor (a natural swap is never refused for a new facet)."""
from core.wallet import abi, lifi_calldata as L
from core.wallet import tx_guard as G
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from tools.defi.providers.routes.lifi import LifiRouteProvider

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
DIAMOND = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
HOLDER = "0x" + "4" * 40
ROGUE = "0x" + "b" * 40


def lifi_data(*, receiver=HOLDER, min_amount=990_000, out=WETH, facet="swapTokensGeneric"):
    step = (DIAMOND, DIAMOND, USDC, out, 2_000_000, b"\x01\x02", False)
    steps_field = L._FACETS[facet]
    steps = [step] if steps_field["type"].endswith("[]") else step
    return abi.encode_call(facet, L._HEAD + [steps_field],
                           [b"\x00" * 32, "polyrob", "", receiver, min_amount, steps])


def test_selectors_are_the_published_lifi_ones():
    assert {n: s for s, (n, _f) in L._by_selector().items()} == {
        "swapTokensGeneric": "0x4630a0d8",
        "swapTokensSingleV3ERC20ToERC20": "0x4666fc80",
        "swapTokensSingleV3ERC20ToNative": "0x733214a3",
        "swapTokensSingleV3NativeToERC20": "0xaf7060fd",
        "swapTokensMultipleV3ERC20ToERC20": "0x5fd9ae2e",
        "swapTokensMultipleV3ERC20ToNative": "0x2c57e884",
        "swapTokensMultipleV3NativeToERC20": "0x736eac0b",
    }


def test_decode_reads_receiver_minimum_and_bought_asset():
    for facet in ("swapTokensGeneric", "swapTokensSingleV3ERC20ToERC20"):
        swap = L.decode(lifi_data(facet=facet))
        assert (swap.receiver, swap.min_amount, swap.receiving_asset) == (HOLDER, 990_000, WETH)


def test_unknown_facet_is_not_refused():
    assert L.decode("0xdeadbeef00") is None
    assert L.floor_refusal("0xdeadbeef", receiver=HOLDER, token_out=WETH, floor=10) is None


def test_floor_refusals():
    ok = dict(receiver=HOLDER, token_out=WETH, floor=990_000)
    assert L.floor_refusal(lifi_data(), **ok) is None
    assert "below the floor" in L.floor_refusal(lifi_data(min_amount=0), **ok)
    assert "below the floor" in L.floor_refusal(lifi_data(min_amount=989_999), **ok)
    assert "pays its output" in L.floor_refusal(lifi_data(receiver=ROGUE), **ok)
    assert "buys" in L.floor_refusal(lifi_data(out=USDC), **ok)
    assert "does not decode" in L.floor_refusal(lifi_data()[:20], **ok)


def _guard(data, floor=990_000):
    intent = G.TxIntent(chain="base", token=USDC, to=DIAMOND, amount_raw=2_000_000,
                        max_spend_usd=5, watch_spenders=(DIAMOND,), inflow_token=WETH,
                        min_inflow_raw=floor)
    deltas = Deltas(ok=True, token_deltas={USDC: -2_000_000, WETH: 995_000}, gas_used=1)
    return G.authorize(intent, dict(to=DIAMOND, value=0, chainId=8453, data=data),
                       holder=HOLDER, gate=PolicyGate(max_per_tx_usd=100, daily_cap_usd=1000),
                       simulate_fn=lambda **kw: deltas, price_fn=lambda *a: 1.0,
                       halted_fn=lambda: False, entry_paused_fn=lambda: False,
                       rpc_is_pinned_fn=lambda c: True)


def test_guard_refuses_a_zero_onchain_minimum_even_when_the_simulation_looks_good():
    result = _guard(lifi_data(min_amount=0))
    assert not result.allowed and "below the floor" in result.reason
    result = _guard(lifi_data(receiver=ROGUE))
    assert not result.allowed and "pays its output" in result.reason


def test_guard_unknown_facet_falls_back_to_the_measured_floor():
    assert _guard("0xdeadbeef").allowed          # measured floor still asserted (6a)
    assert _guard(lifi_data()).allowed
    assert not _guard("0xdeadbeef", floor=996_000).allowed


def _body(data):
    return {"tool": "kyberswap",
            "estimate": {"toAmount": "1000000", "toAmountMin": "990000",
                         "approvalAddress": DIAMOND},
            "transactionRequest": {"to": DIAMOND, "data": data, "value": "0x0",
                                   "chainId": 8453}}


def _quote(data, holder=HOLDER):
    return LifiRouteProvider(fetch=lambda url: _body(data)).quote(
        "base", USDC, WETH, 2_000_000, holder=holder, slippage_bps=150)


def test_route_uses_the_calldata_minimum_not_the_json_one():
    q = _quote(lifi_data(min_amount=0))
    assert q is not None and q.amount_out_min_raw == 0   # best_route then refuses it
    from tools.defi.providers import routes
    assert routes._verified_floor(q, 150) is None
    assert _quote(lifi_data()).amount_out_min_raw == 990_000


def test_route_refuses_calldata_that_pays_someone_else():
    assert _quote(lifi_data(receiver=ROGUE)) is None


def test_route_unknown_facet_keeps_the_json_minimum():
    assert _quote("0xdeadbeef").amount_out_min_raw == 990_000
