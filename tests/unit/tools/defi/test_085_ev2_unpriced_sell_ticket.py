"""085 EV-2: the $5 unchecked-route ticket tests trust on the asset AT RISK.

Before 085 the ticket was lifted for a trusted ``token_out``. For a sell into
the canonical quote asset (USDC, WETH) ``token_out`` is always trusted, so no
sell was ever ticket-capped: a dumped single-pool token could be sold at the
manipulated price with no independent check. Now:

- an untrusted, unpriced sell above $5 goes to the OWNER (D5: never a plain
  refusal, so an exit stays possible), and passes on an owner turn;
- a sell of a trusted token (own launch, owner pin) keeps the lift;
- a priced sell and a small sell are unchanged;
- a buy of a trusted token is unchanged.
"""
import contextlib
import time
import types

import pytest

from core.wallet import token_pins
from core.wallet import token_provenance as tp
from core.wallet.tokens import TokenIdentity
from core.wallet.tx_guard import Decision
from tools.defi import buy_screen
from tools.defi.providers.univ3 import SwapQuote
from tools.defi.trade_tool import DefiTradeTool, SwapParams

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TOK = "0x3535353535353535353535353535353535353535"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
_REAL_OWNER_CONFIRMS = buy_screen.owner_confirms


class _Gate:
    def __init__(self):
        self.recorded = []

    @contextlib.asynccontextmanager
    async def reserve(self):
        yield

    def record(self, **kw):
        self.recorded.append(kw)


class _Signer:
    address = "0x2222222222222222222222222222222222222222"


class _Wallet:
    def __init__(self, gate):
        self.policy = gate

    def operational_signer(self):
        return _Signer()


class _Rail:
    def __init__(self, chain, signer, **kw):
        pass

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", {})
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    monkeypatch.setattr(tp, "provenance_db_path",
                        lambda data_home=None: str(tmp_path / "prov.db"))
    import core.wallet.tokens as tokens_mod
    real = tokens_mod.get_token_identity

    def _ident(chain, address, **kw):
        if address.lower() == TOK.lower():
            return TokenIdentity(chain=chain, address=TOK, symbol="TOK", name="Tok",
                                 decimals=18, verified=False, metadata_changed=False,
                                 source="frozen")
        return real(chain, address, **kw)
    monkeypatch.setattr(tokens_mod, "get_token_identity", _ident)
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 40)
    monkeypatch.setattr(buy_screen, "owner_confirms", lambda *a, **k: False)
    yield
    tp._reset_for_tests()


def _tool(*, tok_price=None):
    gate = _Gate()
    seen = []

    def _guard(intent, tx, **kw):
        seen.append(intent)
        return Decision(allowed=True, reason="test", lane="autonomous", amount_usd=1.0)

    def _route_fn(chain, ti, to_, amt, *, holder, slippage_bps):
        import tools.defi.providers.univ3 as u
        from tools.defi.providers import routes
        from tools.defi.providers.routes.univ3_route import UniV3RouteProvider
        real = u.best_quote
        # 1 TOK (1e18 raw) -> 0.10 USDC (1e5 raw).
        u.best_quote = lambda *a, **k: SwapQuote(
            chain="base", token_in=ti, token_out=to_, amount_in_raw=amt,
            amount_out_raw=max(1, amt // 10 ** 13), fee_tier=500, router=ROUTER,
            quoted_at=time.time())
        try:
            return routes.best_route_with_reason(
                chain, ti, to_, amt, holder=holder, slippage_bps=slippage_bps,
                providers=(UniV3RouteProvider(),))
        finally:
            u.best_quote = real

    prices = {USDC.lower(): 1.0}
    if tok_price is not None:
        prices[TOK.lower()] = tok_price
    tool = DefiTradeTool(
        wallet=_Wallet(gate), rail_factory=_Rail, guard_fn=_guard,
        price_fn=lambda c, a: prices.get(a.lower()),
        fallback_price_fn=lambda c, a: None,
        balance_fn=lambda c, h, t: 10 ** 30, route_fn=_route_fn)
    return tool, seen


async def _sell(tool, *, max_usd=40.0, ctx=None):
    return await tool.swap(SwapParams(
        chain="base", token_in=TOK, token_out=USDC, amount_in=400.0,
        max_spend_usd=max_usd, dry_run=True), execution_context=ctx)


@pytest.mark.asyncio
async def test_an_untrusted_unpriced_sell_above_the_ticket_goes_to_the_owner():
    tool, seen = _tool()
    res = await _sell(tool)
    assert res.error and "owner approval required" in res.error
    assert "owner_queue" in res.error and "refused" not in res.error.split(":")[0]
    assert not seen   # the guard was never reached


@pytest.mark.asyncio
async def test_the_owner_confirming_lets_the_exit_through(monkeypatch):
    monkeypatch.setattr(buy_screen, "owner_confirms", lambda *a, **k: True)
    tool, seen = _tool()
    res = await _sell(tool)
    assert not (res.error and "owner approval required" in res.error), res.error
    assert seen


@pytest.mark.asyncio
async def test_a_small_unpriced_sell_is_unchanged():
    tool, seen = _tool()
    res = await _sell(tool, max_usd=4.0)
    assert not (res.error and "owner approval required" in res.error), res.error
    assert seen


@pytest.mark.asyncio
async def test_a_priced_sell_is_unchanged():
    tool, seen = _tool(tok_price=0.10)
    res = await _sell(tool)
    assert not (res.error and "owner approval required" in res.error), res.error
    assert seen


@pytest.mark.asyncio
async def test_a_sell_of_our_own_launch_keeps_the_lift():
    tp.record_own_token("base", TOK, kind="launchpad_launch", evidence="tx")
    tool, seen = _tool()
    res = await _sell(tool)
    assert not (res.error and "owner approval required" in res.error), res.error
    assert seen


@pytest.mark.asyncio
async def test_a_sell_of_an_owner_pinned_token_keeps_the_lift():
    token_pins.pin("base", TOK, "TOK")
    tool, seen = _tool()
    res = await _sell(tool)
    assert not (res.error and "owner approval required" in res.error), res.error
    assert seen


def test_the_buy_side_ticket_is_unchanged():
    # A trusted buy on an unchecked route is lifted; an untrusted one keeps $5.
    assert buy_screen.unchecked_route_refusal("UNAVAILABLE", 128.0, trusted=True) is None
    assert buy_screen.unchecked_route_refusal("UNAVAILABLE", 128.0, trusted=False)


def test_the_sell_helper_shapes():
    q = buy_screen.unpriced_sell_owner_queue
    assert q("UNAVAILABLE", 40.0, trusted_in=False, owner_ok=False)
    assert q("UNAVAILABLE", 40.0, trusted_in=True, owner_ok=False) is None
    assert q("UNAVAILABLE", 40.0, trusted_in=False, owner_ok=True) is None
    assert q("AGREES", 40.0, trusted_in=False, owner_ok=False) is None
    assert q("UNAVAILABLE", 5.0, trusted_in=False, owner_ok=False) is None
    assert buy_screen.is_sell_into_quote(True, False)
    assert not buy_screen.is_sell_into_quote(True, True)    # USDC -> WETH is a buy
    assert not buy_screen.is_sell_into_quote(False, False)


def test_owner_confirms_fails_closed_without_a_context():
    assert _REAL_OWNER_CONFIRMS(None, None, types.SimpleNamespace(max_spend_usd=1)) is False
