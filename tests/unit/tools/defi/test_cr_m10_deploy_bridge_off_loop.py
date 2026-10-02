"""CR-M10 for the deploy verb and the EVM-origin bridge leg.

`_perform_deploy` called the guard, the broadcast and the receipt poll directly
inside the async action, and the bridge ran `EvmOriginLeg.prepare` (the guard's
simulation RPC) and `EvmOriginLeg.send` (the broadcast RPC) the same way. Each
test makes one of those calls BLOCK the calling thread and counts how often a
concurrent coroutine ran meanwhile.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from tests.unit.tools.defi import test_bridge_evm_origin_e2e as be
from tests.unit.tools.defi import test_deploy_and_call_verbs as dc
from tests.unit.tools.defi.test_bridge_evm_origin_e2e import (  # noqa: F401
    bound_wallet_owner, rig)
from tests.unit.tools.defi.test_deploy_and_call_verbs import _armed  # noqa: F401
from tools.defi import bridge_verb as bv
from tools.defi.trade_tool import DeployTokenParams

BLOCK_SEC = 0.3


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


@pytest.mark.asyncio
async def test_deploy_guard_send_and_receipt_run_off_the_loop(monkeypatch):
    tool, gate = dc._tool()
    real_guard = tool._guard_fn

    def _slow_guard(*a, **kw):
        time.sleep(BLOCK_SEC)
        return real_guard(*a, **kw)

    real_send = dc._Rail.sign_and_send

    def _slow_send(self, tx):
        time.sleep(BLOCK_SEC)
        return real_send(self, tx)

    real_receipt = dc._Rail.await_receipt

    def _slow_receipt(self, tx_hash, **kw):
        time.sleep(BLOCK_SEC)
        return real_receipt(self, tx_hash, **kw)

    tool._guard_fn = _slow_guard
    monkeypatch.setattr(dc._Rail, "sign_and_send", _slow_send)
    monkeypatch.setattr(dc._Rail, "await_receipt", _slow_receipt)
    res, ticks = await _ticks_during(tool.deploy_token(DeployTokenParams(
        chain="base", name="Rob Coin", symbol="ROB", supply=1_000,
        max_spend_usd=5.0, dry_run=False)))
    assert "DEPLOYED AND CONFIRMED" in (res.extracted_content or ""), res.error
    # Three blocking waits of 0.3 s: a frozen loop ticks ~0 times.
    assert ticks >= 30, f"the loop was frozen during the deploy ({ticks} ticks)"


@pytest.mark.asyncio
async def test_bridge_evm_leg_authorize_and_send_run_off_the_loop(rig, monkeypatch):
    def _slow_authorize(intent, tx, **kw):
        time.sleep(BLOCK_SEC)
        return SimpleNamespace(allowed=True, reason="authorized", lane="autonomous",
                               amount_usd=93.6, sim_gas_used=100_000)

    real_send = be._Rail.sign_and_send

    def _slow_send(self, tx):
        time.sleep(BLOCK_SEC)
        return real_send(self, tx)

    monkeypatch.setattr("core.wallet.tx_guard.authorize", _slow_authorize)
    monkeypatch.setattr(be._Rail, "sign_and_send", _slow_send)
    monkeypatch.setattr(bv, "_notify", lambda *a, **k: asyncio.sleep(0))
    res, ticks = await _ticks_during(bv.perform_bridge(
        be._Tool(), be._params(), SimpleNamespace(user_id="owner")))
    assert "BROADCAST: 0xbeef" in (res.content or res.error or "")
    assert ticks >= 20, f"the loop was frozen during the bridge leg ({ticks} ticks)"
