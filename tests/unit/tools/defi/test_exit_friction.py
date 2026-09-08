"""Exit-friction fixes on the trade tool (2026-08-26 untying).

Three frictions the week's logs showed at every cap edge, none of them a
safety property:

1. A confirmed APPROVE consumed the trailing-24h daily cap the SWAP then
   needed — one $2 ticket cost $4 of budget, and at the edge the approve
   landed and the swap refused ("a confirmed approve counts"). The approve is
   a precondition, not a spend: it now records $0 (the check still bounds the
   grant against headroom BEFORE it lands; the swap records the real value).
2. Selling a full position refused on dust rounding (STF: 28.925134 > balance)
   and forced a manual resize dance. A declared amount within 1% above the
   held balance now clamps to the balance — a full exit means "all of it".
3. The approve pre-check refused on sub-cent drift ($1.9903 vs a declared
   $1.99). Cap arithmetic runs in cents.

Plus the wiring for the guard's new exit valuation: the swap intent now
declares its inflow token (token_out), which is what lets tx_guard value an
unpriceable exit at the measured receipt and recognize exit-shaped intents on
the DEFI_MONITOR_EXITS lane.
"""
import contextlib
import time

import pytest

from core.wallet.tx_guard import Decision
from tools.defi.providers.univ3 import SwapQuote
from tools.defi.trade_tool import (ApproveParams, DefiTradeTool, RevokeParams,
                                   SwapParams)

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"


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

    def sign_and_send(self, tx):
        return "0x" + "cd" * 32

    def await_receipt(self, tx_hash, **kw):
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=1,
                       gas_used=50000)


_PRICES = {USDC: 1.0, WETH: 2000.0}


def _tool(*, price=None, captured=None, balance=None, amount_usd=1.0):
    gate = _Gate()

    def _guard(intent, tx, **kw):
        if captured is not None:
            captured.append(intent)
        return Decision(allowed=True, reason="test", lane="autonomous",
                        amount_usd=amount_usd)

    if price is None:
        price_fn = lambda c, a: _PRICES.get(a)          # noqa: E731
    else:
        price_fn = lambda c, a: price                   # noqa: E731

    def _route_fn(chain, ti, to_, amt, *, holder, slippage_bps):
        import tools.defi.providers.univ3 as u
        from tools.defi.providers import routes
        from tools.defi.providers.routes.univ3_route import UniV3RouteProvider
        real = u.best_quote
        # 1 USDC (1e6 raw) -> 0.0005 WETH (5e14 raw) implies $2000/WETH, so the
        # route-sanity check AGREES with the independent price stub.
        u.best_quote = lambda *a, **k: SwapQuote(
            chain="base", token_in=ti, token_out=to_, amount_in_raw=amt,
            amount_out_raw=amt * 500_000_000, fee_tier=500, router=ROUTER,
            quoted_at=time.time())
        try:
            return routes.best_route_with_reason(
                chain, ti, to_, amt, holder=holder, slippage_bps=slippage_bps,
                providers=(UniV3RouteProvider(),))
        finally:
            u.best_quote = real

    return DefiTradeTool(
        wallet=_Wallet(gate), rail_factory=_Rail, guard_fn=_guard,
        price_fn=price_fn, fallback_price_fn=lambda c, a: None,
        balance_fn=lambda c, h, t: balance, route_fn=_route_fn,
    ), gate


# --------------------------------------------------------------------------
# 1. Approve/revoke never consume the daily cap
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_confirmed_approve_records_zero_spend(monkeypatch):
    tool, gate = _tool(amount_usd=1.99)
    res = await tool.approve_token(ApproveParams(
        token=USDC, spender=ROUTER, amount=1.99, max_spend_usd=2.0,
        dry_run=False))
    assert not res.error, res.error
    assert len(gate.recorded) == 1
    assert gate.recorded[0]["action"] == "approve"
    assert gate.recorded[0]["amount_usd"] == 0.0


@pytest.mark.asyncio
async def test_a_confirmed_revoke_records_zero_spend():
    tool, gate = _tool(amount_usd=0.0)
    res = await tool.revoke_approval(RevokeParams(
        token=USDC, spender=ROUTER, dry_run=False))
    assert not res.error, res.error
    assert gate.recorded[0]["amount_usd"] == 0.0


@pytest.mark.asyncio
async def test_a_confirmed_swap_still_records_its_real_value(monkeypatch):
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    tool, gate = _tool(amount_usd=1.99)
    res = await tool.swap(SwapParams(
        token_in=USDC, token_out=WETH, amount_in=1.0, max_spend_usd=2.0,
        dry_run=False))
    assert not res.error, res.error
    assert gate.recorded[0]["action"] == "swap"
    assert gate.recorded[0]["amount_usd"] == 1.99


# --------------------------------------------------------------------------
# 2. The swap intent declares its inflow; full exits clamp dust rounding
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_swap_intent_declares_its_inflow_token(monkeypatch):
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    captured = []
    tool, _ = _tool(captured=captured)
    await tool.swap(SwapParams(
        token_in=USDC, token_out=WETH, amount_in=1.0, max_spend_usd=2.0,
        dry_run=True))
    assert captured and captured[0].inflow_token == WETH


@pytest.mark.asyncio
async def test_a_rounding_overshoot_clamps_to_the_held_balance(monkeypatch):
    """STF live case: sell 28.925134, balance 28.92513399… — refused on dust.
    Within 1%, a full exit means 'all of it'."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    captured = []
    tool, _ = _tool(captured=captured, balance=999_990)
    await tool.swap(SwapParams(
        token_in=USDC, token_out=WETH, amount_in=1.0, max_spend_usd=2.0,
        dry_run=True))
    assert captured and captured[0].amount_raw == 999_990


@pytest.mark.asyncio
async def test_a_real_overshoot_does_not_clamp(monkeypatch):
    """Declaring 1.0 against a 0.90 balance is a wrong size, not rounding —
    the declaration stands and downstream refuses it honestly."""
    import tools.defi.providers.univ3 as u
    monkeypatch.setattr(u, "read_allowance", lambda *a, **k: 10 ** 30)
    captured = []
    tool, _ = _tool(captured=captured, balance=900_000)
    await tool.swap(SwapParams(
        token_in=USDC, token_out=WETH, amount_in=1.0, max_spend_usd=2.0,
        dry_run=True))
    assert captured and captured[0].amount_raw == 1_000_000


# --------------------------------------------------------------------------
# 3. Cents, not sub-cent noise, on the approve pre-check
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_approve_precheck_tolerates_subcent_drift():
    """$1.9906 at-risk vs a declared $1.99 was a live refuse/resize dance."""
    tool, _ = _tool(price=1.0003)
    res = await tool.approve_token(ApproveParams(
        token=USDC, spender=ROUTER, amount=1.99, max_spend_usd=1.99,
        dry_run=True))
    assert not (res.error and "puts $" in res.error), res.error
