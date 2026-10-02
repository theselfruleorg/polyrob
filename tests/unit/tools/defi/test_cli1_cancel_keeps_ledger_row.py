"""CLI1 (2026-10-03 audit): a cancel between broadcast and record must not lose
the ledger row.

A Ctrl-C (or any task cancel) while a money verb waits for its receipt used to
unwind past ``gate.record``: the transaction was already broadcast, but it was
missing from the trailing-24h cap and from ``/book``. The verb now records the
broadcast before the cancel propagates and says the tx may have gone through.
"""
import asyncio
import time

import pytest

from core.wallet.broadcast.evm import Receipt
from tests.unit.tools.defi import test_trade_t4 as t4
from tools.defi.trade_tool import ApproveParams, TransferParams


def _blocking_receipt(self, tx_hash, **kw):
    time.sleep(0.5)
    return Receipt(tx_hash=tx_hash, status="success", block_number=1,
                   gas_used=50_000)


class _BlockingRail(t4._Rail):
    await_receipt = _blocking_receipt

    def build_erc20_transfer(self, *, token, to, amount_raw):
        return self.build_call(to=token, data="0xa9059cbb", value=0)


@pytest.fixture(autouse=True)
def _reset():
    yield
    t4._Rail.last = None


async def _cancel_mid_receipt(coro):
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(0.15)          # broadcast done, receipt wait in progress
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_transfer_cancelled_during_receipt_still_records(monkeypatch, capsys):
    from core.wallet import tokens
    monkeypatch.setattr(tokens, "get_token_identity",
                        lambda c, t: type("I", (), {"decimals": 6, "symbol": "USDC"})())
    tool, gate = t4._tool()
    tool._rail_factory = _BlockingRail
    await _cancel_mid_receipt(tool.transfer(TransferParams(
        token=t4.USDC, to="0x" + "3" * 40, amount=1.0, max_spend_usd=2.0,
        dry_run=False)))
    assert len(gate.recorded) == 1, gate.recorded
    row = gate.recorded[0]
    assert row["action"] == "transfer"
    assert row["result_ref"]
    assert row["amount_usd"] == 1.0
    err = capsys.readouterr().err
    assert "interrupted" in err and "may have gone through" in err
    assert row["result_ref"] in err


@pytest.mark.asyncio
async def test_run_guarded_cancelled_during_receipt_still_records(capsys):
    tool, gate = t4._tool()
    tool._rail_factory = _BlockingRail
    await _cancel_mid_receipt(tool.approve_token(ApproveParams(
        token=t4.USDC, spender=t4.ROUTER, amount=1.0, max_spend_usd=2.0,
        dry_run=False)))
    assert len(gate.recorded) == 1, gate.recorded
    assert gate.recorded[0]["action"] == "approve"
    assert "may have gone through" in capsys.readouterr().err
