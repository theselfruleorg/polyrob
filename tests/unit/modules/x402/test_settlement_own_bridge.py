"""A bridge's arrival into the treasury is OUR money, not an unmatched payment.

Prod 2026-10-04 14:57: the owner approved a bridge (0.15 ETH robinhood -> USDC
on Base). Phase 2 measured +403.149152 USDC landing in the treasury, and one
watcher tick later the settlement watcher reported that same transfer — sent by
the bridge's solver, an address that is neither ours nor a swap router — as a
payment that "matched NO pending invoice, owner should reconcile".

The correlation is against the recorded bridge row (`bridges.db`): same
destination chain, recipient and asset, and the EXACT measured arrival (or, for
a bridge not yet measured, an amount inside a tight band above its guaranteed
floor). Anything else stays an owner notice — a real payment must never vanish.
"""
import time
from types import SimpleNamespace

import pytest

from core.wallet import bridge_guard
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402.settlement_watcher import SettlementWatcher

TREASURY = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLVER = "0xf70da97812cb96acdf810712aa562db8dfa3dbef"
BASE_ID = 8453
FLOOR = 395_084_374
ARRIVED = 403_149_152


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


@pytest.fixture
def _emitted(monkeypatch):
    events = []
    from modules.x402 import invoicing
    monkeypatch.setattr(invoicing, "_emit",
                        lambda kind, **kw: events.append((kind, kw)))
    return events


def _bridge(*, state=bridge_guard.STATE_ARRIVED, before=3_259_753,
            after=3_259_753 + ARRIVED, currency=USDC_BASE, dest=BASE_ID):
    quote = SimpleNamespace(
        request_id=f"0xreq{time.time_ns()}", origin_chain_id=4663,
        dest_chain_id=dest, recipient=TREASURY, currency_out=currency,
        amount_in_raw=150_000_000_000_000_000, min_out_raw=FLOOR)
    bid = bridge_guard.record_pending(user_id="rob", quote=quote,
                                      amount_usd=404.21, balance_before=before)
    if state != bridge_guard.STATE_PENDING:
        bridge_guard.settle(bid, state=state, balance_after=after)
    return bid


def _transfer(raw=ARRIVED):
    return {"tx_hash": "0xbe12", "from": SOLVER, "amount_raw": raw,
            "amount_usd": raw / 1e6, "block": 1, "log_index": 0}


# -- the lookup ------------------------------------------------------------

def test_the_measured_arrival_is_recognized():
    bid = _bridge()
    assert bridge_guard.own_bridge_arrival(
        dest_chain_id=BASE_ID, recipient=TREASURY.lower(), currency=USDC_BASE,
        amount_raw=ARRIVED) == bid


def test_an_in_flight_bridge_claims_an_amount_just_above_its_floor():
    bid = _bridge(state=bridge_guard.STATE_IN_FLIGHT, after=None)
    assert bridge_guard.own_bridge_arrival(
        dest_chain_id=BASE_ID, recipient=TREASURY, currency=USDC_BASE,
        amount_raw=ARRIVED) == bid


def test_an_arrived_bridge_claims_only_its_exact_measured_amount():
    _bridge()
    assert bridge_guard.own_bridge_arrival(
        dest_chain_id=BASE_ID, recipient=TREASURY, currency=USDC_BASE,
        amount_raw=ARRIVED + 1) is None


@pytest.mark.parametrize("raw", [FLOOR - 1, FLOOR * 2])
def test_an_in_flight_bridge_claims_nothing_outside_its_band(raw):
    _bridge(state=bridge_guard.STATE_IN_FLIGHT, after=None)
    assert bridge_guard.own_bridge_arrival(
        dest_chain_id=BASE_ID, recipient=TREASURY, currency=USDC_BASE,
        amount_raw=raw) is None


def test_wrong_chain_recipient_or_asset_is_not_ours():
    _bridge()
    for kw in ({"dest_chain_id": 1}, {"recipient": "0x" + "1" * 40},
               {"currency": "0x" + "2" * 40}):
        args = dict(dest_chain_id=BASE_ID, recipient=TREASURY,
                    currency=USDC_BASE, amount_raw=ARRIVED)
        args.update(kw)
        assert bridge_guard.own_bridge_arrival(**args) is None


def test_a_failed_bridge_claims_nothing():
    _bridge(state=bridge_guard.STATE_FAILED)
    assert bridge_guard.own_bridge_arrival(
        dest_chain_id=BASE_ID, recipient=TREASURY, currency=USDC_BASE,
        amount_raw=ARRIVED) is None


def test_the_lookup_never_creates_the_store(tmp_path):
    assert bridge_guard.own_bridge_arrival(
        dest_chain_id=BASE_ID, recipient=TREASURY, currency=USDC_BASE,
        amount_raw=ARRIVED) is None
    assert not (tmp_path / "home" / "bridges.db").exists()


# -- the watcher -----------------------------------------------------------

async def _watcher(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()

    class _Agent:
        async def deliver_self_wake(self, *a, **kw):
            return True
    return SettlementWatcher(_Agent(), db=db), db


@pytest.mark.asyncio
async def test_our_bridge_arrival_is_not_an_unmatched_payment(tmp_path, _emitted):
    bid = _bridge()
    watcher, db = await _watcher(tmp_path)
    try:
        settled, unmatched = await watcher._settle_or_flag(
            [_transfer()], TREASURY.lower(), "base", asset_address=USDC_BASE)
    finally:
        await db.close()
    assert (settled, unmatched) == (0, 0)
    kinds = [k for k, _ in _emitted]
    assert "payment_unmatched" not in kinds
    crumbs = [kw for k, kw in _emitted if k == "payment_self_proceeds"]
    assert crumbs and crumbs[0]["attrs"]["correlation"] == f"own_bridge:{bid}"


@pytest.mark.asyncio
async def test_a_stranger_paying_while_a_bridge_landed_is_still_reported(
        tmp_path, _emitted):
    _bridge()
    watcher, db = await _watcher(tmp_path)
    try:
        settled, unmatched = await watcher._settle_or_flag(
            [_transfer(raw=12_000_000)], TREASURY.lower(), "base",
            asset_address=USDC_BASE)
    finally:
        await db.close()
    assert unmatched == 1
    assert "payment_unmatched" in [k for k, _ in _emitted]
