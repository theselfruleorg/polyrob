"""CLI1 on every money path (2026-10-03 interface audit follow-up).

836badcb1 fixed the trade tool: a cancel (Ctrl-C, task cancel) while a money verb
waits for its receipt records the broadcast against the cap and ``/book`` before it
unwinds. The other verbs that wait BEFORE they record — launchpad, agent-NFT,
collection reveal, LP, call, deploy — had the same hole. One test per path shape;
all of them ride the ONE seam, ``tools/defi/receipt_wait.py``.

The bridge is not here on purpose: it records right after the send, inside the
reservation, BEFORE it waits for the origin receipt, and an open bridge row stays
``pending`` for the arrival watcher.
"""
import asyncio
import time

import pytest

from core.wallet.broadcast.evm import Receipt
from tests.unit.tools.defi import test_deploy_and_call_verbs as dc
from tests.unit.tools.defi import test_lp_verbs as lp
from tests.unit.tools.defi.test_lp_verbs import reads  # noqa: F401  (LP read stubs + flag)
from tests.unit.tools.defi import test_trade_t4 as t4

WAIT = 0.5


def _blocking_receipt(self, tx_hash, **kw):
    time.sleep(WAIT)
    return Receipt(tx_hash=tx_hash, status="success", block_number=1, gas_used=50_000)


async def _cancel_mid_receipt(coro):
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(0.15)          # broadcast done, receipt wait in progress
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _said_so(capsys, tx_ref):
    err = capsys.readouterr().err
    assert "interrupted" in err and "may have gone through" in err
    assert tx_ref in err


@pytest.fixture(autouse=True)
def _reset():
    yield
    t4._Rail.last = None


@pytest.mark.asyncio
async def test_launchpad_guarded_send(capsys):
    from tools.launchpad.execute import guarded_send

    class _R(t4._Rail):
        await_receipt = _blocking_receipt

    tool, gate = t4._tool()
    tool._rail_factory = _R
    tool._notify_tx = lambda *a, **k: None
    await _cancel_mid_receipt(guarded_send(
        tool, execution_context=None, verb="buy", chain="base",
        to="0x" + "4" * 40, calldata="0x12345678", value_wei=10 ** 12,
        max_spend_usd=2.0, dry_run=False, header=""))
    assert len(gate.recorded) == 1, gate.recorded
    assert gate.recorded[0]["action"] == "launchpad_buy"
    _said_so(capsys, gate.recorded[0]["result_ref"])


@pytest.mark.asyncio
async def test_lp_add(monkeypatch, capsys):
    monkeypatch.setattr(lp.R, 'pool_address', lambda *a: lp.POOL)

    class _R(lp._Rail):
        await_receipt = _blocking_receipt

    tool, gate = lp.tool()
    tool._rail_factory = _R
    await _cancel_mid_receipt(tool.lp_add(lp.add(dry_run=False)))
    assert len(gate.recorded) == 1, gate.recorded
    assert gate.recorded[0]["action"] == "lp_add"
    _said_so(capsys, gate.recorded[0]["result_ref"])


@pytest.mark.asyncio
async def test_call(monkeypatch, capsys):
    monkeypatch.setenv("DEFI_CALL_ENABLED", "true")
    monkeypatch.setattr(dc._Rail, "await_receipt", _blocking_receipt)
    tool, gate = dc._tool(facts=None)
    await _cancel_mid_receipt(tool.call(dc.CallParams(
        chain="base", to=dc.POOL, calldata="0xdeadbeef", max_spend_usd=5.0,
        dry_run=False)))
    assert len(gate.recorded) == 1, gate.recorded
    assert gate.recorded[0]["action"] == "call"
    _said_so(capsys, gate.recorded[0]["result_ref"])


@pytest.mark.asyncio
async def test_deploy(monkeypatch, capsys):
    monkeypatch.setenv("DEFI_DEPLOY_ENABLED", "true")
    monkeypatch.setattr(dc._Rail, "await_receipt", _blocking_receipt)
    tool, gate = dc._tool()
    await _cancel_mid_receipt(tool.deploy_token(dc.DeployTokenParams(
        chain="base", name="Rob Coin", symbol="ROB", supply=1_000,
        max_spend_usd=5.0, dry_run=False)))
    assert len(gate.recorded) == 1, gate.recorded
    assert gate.recorded[0]["action"] == "deploy_token"
    _said_so(capsys, gate.recorded[0]["result_ref"])


@pytest.mark.asyncio
async def test_agent_nft_guarded_call(monkeypatch, tmp_path, capsys):
    import os

    from core.wallet.signer import LocalEoaSigner
    from tests.collection_pins import pin, profile
    from tests.unit.tools import test_agent_nft_withdraw as wd
    from tools.agent_nft.tool import TakeParams

    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    pin(monkeypatch, profile(wd.PINNED, journal_prefix="POLYROB"))
    monkeypatch.setattr(wd.FakeRail, "await_receipt", _blocking_receipt)
    wd.FakeRail.sent = []
    signer = LocalEoaSigner(os.urandom(32))
    tool, _ = wd._tool(signer, wd.Chain(signer.address))
    tool._impl = lambda: None
    await _cancel_mid_receipt(tool.agent_nft_withdraw_token(
        TakeParams(to=wd.TO, nft="3", dry_run=False)))
    rows = [r for r in tool._get_wallet().policy.audit_log if r.get("action")]
    assert [r["action"] for r in rows] == ["agent_nft_withdraw_token"], rows
    _said_so(capsys, "0x" + "ef" * 32)


@pytest.mark.asyncio
async def test_agent_nft_collection_reveal(monkeypatch, capsys):
    from tests.collection_pins import pin
    from tests.unit.tools import test_agent_nft_collection_reveal as rv
    from tools.agent_nft.tool import RevealParams

    monkeypatch.setenv("AGENT_NFT_ENABLED", "true")
    monkeypatch.delenv(rv.V.MAX_GAS_FLAG, raising=False)
    pin(monkeypatch, rv.COLLECTION)
    monkeypatch.setattr(rv.FakeRail, "await_receipt", _blocking_receipt)
    rv.FakeRail.sent = []
    tool, _ = rv._tool(rv.Chain(head=100, blocks={1: 7}))
    await _cancel_mid_receipt(tool.agent_nft_collection_reveal(RevealParams(dry_run=False)))
    log = tool._get_wallet().policy.audit_log
    assert [r["action"] for r in log] == ["agent_nft_collection_reveal"], log
    assert log[-1]["amount_usd"] == pytest.approx(0.02)   # the worst-case fee: never under
    _said_so(capsys, "0x" + "ab" * 32)
