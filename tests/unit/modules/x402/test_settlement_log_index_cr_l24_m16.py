"""CR-L24: a second Transfer log in an already-settled tx is reported, never
dropped. CR-M16: the settled row stores the payer; a transfer from the
agent's own address never settles an invoice."""
import json

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing
from modules.x402.settlement_watcher import SettlementWatcher
import modules.x402.settlement_scan as scan

TREASURY = "0xTreasuryAddress000000000000000000000001"
OWN = "0x" + "ab" * 20


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_SETTLE_ONCHAIN_DETECT", "X402_INVOICE_AMOUNT_JITTER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(scan, "_own_evm_addresses", lambda: frozenset({OWN}))


async def _db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


class _W(SettlementWatcher):
    def __init__(self, db):
        super().__init__(object(), db=db)
        self.unmatched_notices = []

    async def _notify_unmatched(self, transfer, treasury):
        self.unmatched_notices.append(transfer)


async def _amount(db, rid):
    r = await db.fetch_one("SELECT amount_usd FROM x402_payment_requests WHERE id=?", (rid,))
    return float(r["amount_usd"])


@pytest.mark.asyncio
async def test_second_log_in_a_settled_tx_is_reported_not_dropped(tmp_path):
    db = await _db(tmp_path)
    try:
        a = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=5.0, purpose="a", db=db)
        b = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=7.0, purpose="b", db=db)
        w = _W(db)
        t1 = {"tx_hash": "0xmulti", "from": "0x" + "11" * 20,
              "amount_usd": await _amount(db, a["request_id"]), "block": 1, "log_index": 3}
        t2 = {"tx_hash": "0xmulti", "from": "0x" + "11" * 20,
              "amount_usd": await _amount(db, b["request_id"]), "block": 1, "log_index": 4}
        settled, unmatched = await w._settle_or_flag([t1, t2], TREASURY.lower())
        assert (settled, unmatched) == (1, 1)
        assert w.unmatched_notices == [t2]
        # A replay of the SAME log stays silent.
        w.unmatched_notices.clear()
        settled, unmatched = await w._settle_or_flag([t1], TREASURY.lower())
        assert (settled, unmatched) == (0, 0) and w.unmatched_notices == []
        row = await db.fetch_one(
            "SELECT metadata FROM x402_payment_requests WHERE id=?", (a["request_id"],))
        meta = row["metadata"]
        meta = meta if isinstance(meta, dict) else json.loads(meta)
        assert meta["payer_address"] == "0x" + "11" * 20
        assert meta["settled_log_index"] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_transfer_from_own_address_never_settles(tmp_path):
    db = await _db(tmp_path)
    try:
        a = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=5.0, purpose="a", db=db)
        w = _W(db)
        t = {"tx_hash": "0xself", "from": OWN.upper().replace("0X", "0x"),
             "amount_usd": await _amount(db, a["request_id"]), "block": 1, "log_index": 0}
        settled, unmatched = await w._settle_or_flag([t], TREASURY.lower())
        assert (settled, unmatched) == (0, 0)
        row = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id=?", (a["request_id"],))
        assert row["status"] == "pending"
    finally:
        await db.close()
