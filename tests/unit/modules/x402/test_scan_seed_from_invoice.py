"""098 W1 MO-06: the scan of a group seen for the first time starts at the
block the oldest pending invoice could first be paid in (not "where the scanner
first looked"), and a rotated treasury's pending invoices are still scanned."""
import time

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing
from modules.x402.settlement_watcher import SettlementWatcher
from tests.unit.modules.x402.test_settlement_watcher_onchain import USDC, _FakeChain, _log

OLD = "0xTreasuryAddress000000000000000000000001"
NEW = "0xTreasuryAddress000000000000000000000002"


class _TimedChain(_FakeChain):
    """Block n was mined at t0 + 2n."""

    def __init__(self, head, t0):
        super().__init__(head=head)
        self.t0 = t0

    def rpc(self, method, params):
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(int(self.t0 + 2 * int(params[0], 16)))}
        return super().rpc(method, params)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", OLD)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "false")
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_SETTLEMENT_CONFIRMATIONS", "0")


async def _db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


class _Agent:
    container = None

    async def deliver_self_wake(self, *a, **k):
        return True


async def _status(db, rid):
    return (await db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?",
                               (rid,)))["status"]


@pytest.mark.asyncio
async def test_new_asset_payment_before_first_tick_is_matched(tmp_path):
    db = await _db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="alice", session_id="s", amount_usd=12.34, purpose="w", db=db)
        # the invoice was minted around block 900; the payer paid at block 950;
        # the watcher first looks at head 1000
        chain = _TimedChain(head=1000, t0=time.time() - 1800)
        chain.logs = [_log("0xpay", "0xPayer0000000000000000000000000000000003",
                           12_340000, 950, to_addr=OLD)]
        w = SettlementWatcher(_Agent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC)
        await w.tick_once()          # seeds at the invoice's block, scans nothing
        chain.head = 1001
        out = await w.tick_once()
        assert out["onchain_settled"] == 1
        assert await _status(db, inv["request_id"]) == "completed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_old_treasury_invoice_still_scanned_after_rotation(tmp_path, monkeypatch):
    db = await _db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="alice", session_id="s", amount_usd=7.0, purpose="w", db=db)
        monkeypatch.setenv("X402_PAYMENT_RECIPIENT", NEW)     # the operator rotates
        from api.x402_advertisement import reset_treasury_cache
        reset_treasury_cache()
        chain = _TimedChain(head=1000, t0=time.time() - 1800)
        w = SettlementWatcher(_Agent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC)
        await w.tick_once()
        chain.head = 1010
        chain.logs = [_log("0xold", "0xPayer0000000000000000000000000000000004",
                           7_000000, 1005, to_addr=OLD)]
        out = await w.tick_once()
        assert out["onchain_settled"] == 1
        assert await _status(db, inv["request_id"]) == "completed"
    finally:
        await db.close()
