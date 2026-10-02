"""CR-M10 for the generic contract call: the receipt wait must not block the loop."""
import asyncio
import time

import pytest

from tests.unit.tools.defi import test_deploy_and_call_verbs as dc
from tests.unit.tools.defi.test_deploy_and_call_verbs import POOL, _armed  # noqa: F401
from tools.defi.trade_tool import CallParams

BLOCK_SEC = 0.4


@pytest.mark.asyncio
async def test_a_blocking_receipt_wait_leaves_the_loop_running(monkeypatch):
    def _slow(self, tx_hash, **kw):
        time.sleep(BLOCK_SEC)
        from core.wallet.broadcast.evm import Receipt
        return Receipt(tx_hash=tx_hash, status="success", block_number=7,
                       gas_used=500_000)
    monkeypatch.setattr(dc._Rail, "await_receipt", _slow)
    tool, _ = dc._tool()
    ticks = 0
    done = False

    async def ticker():
        nonlocal ticks
        while not done:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        res = await tool.call(CallParams(chain="base", to=POOL,
                                         calldata="0xdeadbeef" + "00" * 32,
                                         value=0.001, max_spend_usd=5.0,
                                         dry_run=False))
    finally:
        done = True
        await t
    assert res.error is None, res.error
    assert ticks >= 10, f"event loop starved during the receipt wait ({ticks} ticks)"
