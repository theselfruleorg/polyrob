"""A TRUSTED buy's route is checked against the exit-grade price (DEFI-6, natural flow).

Prod's owner-authored buyback swaps 0.05 WETH (~$128) into the instance's own
launch (PNL). PNL has no spend-grade price, so the route check read UNAVAILABLE
and the $5 unchecked-route ticket refused every buyback. For a trusted token
(owner pin, own launch, owner target) the route is now checked against the
liquidity-backed exit-grade price instead: a fair route AGREES and passes, a
lying quote DISAGREES, and an untrusted token keeps the $5 ticket."""
import time
import types

import pytest

from core.wallet import token_pins
from core.wallet import token_provenance as tp
from tools.defi.buy_screen import unchecked_route_refusal
from tools.defi.identity_gate import trusted_buy
from tools.defi.providers.routes import RouteQuote
from tools.defi.trade_tool import DefiTradeTool

WETH = "0x4200000000000000000000000000000000000006"
PNL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
ROUTER = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
ETH_USD, PNL_USD = 2560.0, 0.001
IDENT = types.SimpleNamespace(decimals=18, symbol="PNL")


def _tool():
    return DefiTradeTool(
        price_fn=lambda c, a: ETH_USD if a.lower() == WETH.lower() else None,
        fallback_price_fn=lambda c, a: PNL_USD if a.lower() == PNL.lower() else None)


def _route(out_factor=1.0):
    # 0.05 WETH = $128 -> 128,000 PNL at $0.001
    out = int(128_000 * out_factor) * 10 ** 18
    return RouteQuote(chain="robinhood", token_in=WETH, token_out=PNL,
                      amount_in_raw=5 * 10 ** 16, amount_out_raw=out,
                      amount_out_min_raw=out * 99 // 100, spender=ROUTER, to=ROUTER,
                      calldata="0x", value_raw=0, venue="lifi",
                      quoted_at=time.time(), locally_built=False)


def test_a_trusted_buyback_with_a_fair_route_agrees_and_is_not_ticket_capped():
    verdict, note = _tool()._route_sanity("robinhood", _route(), IDENT, IDENT,
                                          slippage_bps=100, out_trusted=True)
    assert verdict == "AGREES", note
    assert unchecked_route_refusal(verdict, 128.0) is None


def test_a_lying_quote_for_a_trusted_token_still_disagrees():
    verdict, note = _tool()._route_sanity("robinhood", _route(out_factor=0.01), IDENT,
                                          IDENT, slippage_bps=100, out_trusted=True)
    assert verdict == "DISAGREES", note


def test_an_untrusted_token_keeps_the_unchecked_ticket():
    verdict, _ = _tool()._route_sanity("robinhood", _route(), IDENT, IDENT,
                                       slippage_bps=100, out_trusted=False)
    assert verdict == "UNAVAILABLE"
    assert unchecked_route_refusal(verdict, 128.0)


@pytest.fixture
def _stores(tmp_path, monkeypatch):
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", {})
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    monkeypatch.setattr(tp, "provenance_db_path",
                        lambda data_home=None: str(tmp_path / "prov.db"))
    tp.record_own_token("robinhood", PNL, kind="launchpad_launch", evidence="tx")
    yield
    tp._reset_for_tests()


def _ctx():
    return types.SimpleNamespace(user_id="rob", metadata={}, role="orchestrator",
                                 is_sub_agent=False)


def test_trust_follows_own_launch_and_pins_only(_stores):
    unverified = types.SimpleNamespace(symbol="PNL", verified=False, source="frozen")
    assert trusted_buy(unverified, chain="robinhood", token_out=PNL, execution_context=_ctx())
    assert not trusted_buy(unverified, chain="robinhood", token_out=FAKE,
                           execution_context=_ctx())
    token_pins.pin("robinhood", FAKE, "OTHER")
    assert trusted_buy(unverified, chain="robinhood", token_out=FAKE,
                       execution_context=_ctx())
