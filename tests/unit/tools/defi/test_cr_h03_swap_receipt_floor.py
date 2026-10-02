"""CR-H03 (2026-09-23): a swap's output is asserted against its floor.

EVM swaps built their TxIntent without ``min_inflow_raw``, so a route that took
token_in and returned nothing passed the real guard. The route seam and the
Jupiter provider also took the pair and the amount from the provider's payload
without checking it was the trade that was asked for.
"""
import time

import pytest

from core.wallet import simulation
from core.wallet.simulation import Deltas
from tests.unit.tools.defi.test_trade_real_guard import (
    USDC, WETH, _Rail, _genuine_ctx, _swap_sim, _tool)
from tools.defi.providers import jupiter, routes
from tools.defi.providers.routes import RouteQuote
from tools.defi.trade_tool import SwapParams

ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"


def _nothing_back_sim(**kw):
    d = _swap_sim(**kw)
    token_in = kw["tokens"][0]
    return Deltas(ok=True, native_delta=0,
                  token_deltas={token_in: -1_000_000, WETH: 0},
                  allowance_deltas=d.allowance_deltas,
                  holder_transfers=d.holder_transfers, gas_used=140_000)


@pytest.fixture(autouse=True)
def _allowance(monkeypatch):
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    yield
    _Rail.last = None


@pytest.mark.asyncio
async def test_a_swap_that_returns_nothing_is_refused_by_the_real_guard(monkeypatch):
    monkeypatch.setattr(simulation, "simulate", _nothing_back_sim)
    tool, _ = _tool()
    res = await tool.swap(SwapParams(token_in=USDC, token_out=WETH, amount_in=1.0,
                                     max_spend_usd=2.0, dry_run=False),
                          execution_context=_genuine_ctx())
    text = (res.extracted_content or "") + (res.error or "")
    assert "NOT SENT" in text, text
    assert not _Rail.last.sent


@pytest.mark.asyncio
async def test_the_swap_intent_carries_the_route_floor(monkeypatch):
    captured = []
    monkeypatch.setattr(simulation, "simulate", _swap_sim)
    tool, _ = _tool()
    real = tool._guard_fn

    def spy(intent, tx, **kw):
        captured.append(intent)
        from core.wallet import tx_guard
        return tx_guard.authorize(intent, tx, **kw)
    tool._guard_fn = spy
    res = await tool.swap(SwapParams(token_in=USDC, token_out=WETH, amount_in=1.0,
                                     max_spend_usd=2.0),
                          execution_context=_genuine_ctx())
    assert res.error is None, res.error
    assert real is None
    i = captured[0]
    assert i.inflow_token == WETH
    # 500e12 quoted out at the default 100 bps slippage.
    assert i.min_inflow_raw == 500_000_000_000_000 * 9_900 // 10_000


class _Prov:
    name = "fake"

    def __init__(self, **over):
        self.over = over

    def supports(self, chain):
        return True

    def quote(self, chain, ti, to, amt, *, holder, slippage_bps):
        base = dict(chain=chain, token_in=ti, token_out=to, amount_in_raw=amt,
                    amount_out_raw=1000, amount_out_min_raw=990, spender=ROUTER,
                    to=ROUTER, calldata="0x12345678", value_raw=0, venue="v",
                    quoted_at=time.time(), locally_built=True)
        return RouteQuote(**(base | self.over))


@pytest.mark.parametrize("over", [dict(token_in=WETH), dict(token_out=USDC),
                                  dict(amount_in_raw=999)])
def test_a_route_for_another_trade_is_refused(over):
    route, why = routes.best_route_with_reason(
        "base", USDC, WETH, 1000, holder="0x" + "2" * 40, slippage_bps=100,
        providers=(_Prov(**over),))
    assert route is None


def test_a_route_for_the_requested_trade_passes():
    route, why = routes.best_route_with_reason(
        "base", USDC, WETH, 1000, holder="0x" + "2" * 40, slippage_bps=100,
        providers=(_Prov(),))
    assert route is not None, why


def _payload(**over):
    base = {"inputMint": "A", "outputMint": "B", "inAmount": "1000",
            "outAmount": "1000", "otherAmountThreshold": "990",
            "swapMode": "ExactIn"}
    return base | over


def test_jupiter_refuses_a_quote_for_another_trade():
    assert jupiter.quote("A", "B", 1000, slippage_bps=100,
                         fetch=lambda url: _payload()) is not None
    for over in (dict(inputMint="X"), dict(outputMint="X"), dict(inAmount="7")):
        assert jupiter.quote("A", "B", 1000, slippage_bps=100,
                             fetch=lambda url, o=over: _payload(**o)) is None


def test_jupiter_refuses_an_exact_out_quote():
    assert jupiter.parse_quote(_payload(swapMode="ExactOut"), chain="solana") is None
