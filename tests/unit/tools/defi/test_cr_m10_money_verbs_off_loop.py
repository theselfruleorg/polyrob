"""CR-M10 (2026-09-23): money verbs must not block the event loop.

Receipt polling (`time.sleep` for up to 120 s), the guard's simulation RPC and
the broadcast RPC ran directly inside async actions, so one money verb froze
`/stop`, Telegram and every other session for as long as it waited. Each test
runs a verb whose receipt wait BLOCKS the calling thread and counts how often a
concurrent coroutine ran meanwhile.
"""
import asyncio
import time

import pytest

from core.wallet.broadcast.evm import Receipt
from tests.unit.tools.defi import test_lp_verbs as lp
from tests.unit.tools.defi.test_lp_verbs import reads  # noqa: F401  (LP read stubs + flag)
from tests.unit.tools.defi import test_trade_t4 as t4
from tools.defi.trade_tool import ApproveParams, TransferParams

BLOCK_SEC = 0.4


def _slow_receipt(self, tx_hash, **kw):
    time.sleep(BLOCK_SEC)            # the real rail's blocking poll
    return Receipt(tx_hash=tx_hash, status="success", block_number=1,
                   gas_used=50_000)


async def _ticks_during(coro):
    ticks = 0
    done = False

    async def ticker():
        nonlocal ticks
        while not done:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        done = True
        await task
    return result, ticks


@pytest.fixture(autouse=True)
def _reset():
    yield
    t4._Rail.last = None


class _SlowT4Rail(t4._Rail):
    await_receipt = _slow_receipt

    def build_erc20_transfer(self, *, token, to, amount_raw):
        return self.build_call(to=token, data="0xa9059cbb", value=0)


@pytest.mark.asyncio
async def test_run_guarded_waits_for_the_receipt_off_the_loop():
    tool, gate = t4._tool()
    tool._rail_factory = _SlowT4Rail
    res, ticks = await _ticks_during(tool.approve_token(ApproveParams(
        token=t4.USDC, spender=t4.ROUTER, amount=1.0, max_spend_usd=2.0,
        dry_run=False)))
    assert res.error is None, res.error
    assert ticks >= 10, f"the loop was frozen during the receipt wait ({ticks} ticks)"


@pytest.mark.asyncio
async def test_transfer_waits_for_the_receipt_off_the_loop(monkeypatch):
    from core.wallet import tokens
    monkeypatch.setattr(tokens, "get_token_identity",
                        lambda c, t: type("I", (), {"decimals": 6, "symbol": "USDC"})())
    tool, gate = t4._tool()
    tool._rail_factory = _SlowT4Rail
    res, ticks = await _ticks_during(tool.transfer(TransferParams(
        token=t4.USDC, to="0x" + "3" * 40, amount=1.0, max_spend_usd=2.0,
        dry_run=False)))
    assert res.error is None, res.error
    assert ticks >= 10, f"the loop was frozen during the receipt wait ({ticks} ticks)"


@pytest.mark.asyncio
async def test_launchpad_guarded_send_waits_off_the_loop():
    from tools.launchpad.execute import guarded_send
    tool, gate = t4._tool()
    tool._rail_factory = _SlowT4Rail
    tool._notify_tx = lambda *a, **k: None
    res, ticks = await _ticks_during(guarded_send(
        tool, execution_context=None, verb="buy", chain="base",
        to="0x" + "4" * 40, calldata="0x12345678", value_wei=10 ** 12,
        max_spend_usd=2.0, dry_run=False, header=""))
    assert res.error is None, res.error
    assert ticks >= 10, f"the loop was frozen during the receipt wait ({ticks} ticks)"


@pytest.mark.asyncio
async def test_lp_waits_for_the_receipt_off_the_loop(monkeypatch):
    monkeypatch.setattr(lp.R, 'pool_address', lambda *a: lp.POOL)

    class _SlowLpRail(lp._Rail):
        await_receipt = _slow_receipt

    tool, gate = lp.tool()
    tool._rail_factory = _SlowLpRail
    res, ticks = await _ticks_during(tool.lp_add(lp.add(dry_run=False)))
    assert res.error is None, res.error
    assert ticks >= 10, f"the loop was frozen during the receipt wait ({ticks} ticks)"
