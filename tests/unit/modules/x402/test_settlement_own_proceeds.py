"""An inbound transfer is skipped only when it is CONFIRMED to be our own
trade proceeds — never because of the address it came from.

`_settle_or_flag` skipped (no settlement, no `payment_unmatched`, no owner
notice, no telemetry) any unmatched inbound USDC transfer whose `from` was the
chain's Uniswap router or LI.FI aggregator spender. Those are SHARED PUBLIC
contracts used by every wallet on the chain, so the heuristic also swallowed:

* a genuine payer who happened to route their payment through the same router;
* an overpayment or a rounding difference, where the exact-amount match misses
  and the fallback should have been an owner notice.

The correlation is now against the agent's OWN recorded trades (the wallet
audit ledger's `result_ref`, which for a swap is exactly the transaction that
carries the proceeds home), and an uncorrelated router-sourced transfer falls
back to the unmatched-payment path.
"""
import json

import pytest

from core.wallet import trade_index
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402.settlement_watcher import SettlementWatcher

TREASURY = "0xTreasuryAddress000000000000000000000001"
# Base's Uniswap SwapRouter02 / the LI.FI diamond — public infrastructure.
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"


class _WakeAgent:
    def __init__(self):
        self.delivered = []

    async def deliver_self_wake(self, *a, **kw):
        self.delivered.append((a, kw))
        return True


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))


def _write_audit(tmp_path, entries):
    ledger = tmp_path / "home" / "wallet" / "audit.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps(e) + "\n" for e in entries),
                      encoding="utf-8")
    return ledger


@pytest.fixture
def _emitted(monkeypatch):
    events = []
    from modules.x402 import invoicing
    real = invoicing._emit
    monkeypatch.setattr(invoicing, "_emit",
                        lambda kind, **kw: events.append((kind, kw)))
    assert real is not None
    return events


# -- the lookup ------------------------------------------------------------

def test_a_recorded_trade_is_recognized_by_its_broadcast_hash(tmp_path):
    _write_audit(tmp_path, [{"venue": "defi", "action": "swap",
                             "result_ref": "0xOURSWAP"}])
    assert trade_index.is_own_trade_tx("0xourswap") is True
    assert trade_index.is_own_trade_tx("0xSOMEONEELSE") is False


def test_a_missing_ledger_confirms_nothing(tmp_path):
    """Absent evidence is not evidence of ownership — the caller must fall
    back to notifying the owner."""
    assert trade_index.own_trade_tx_refs() == set()
    assert trade_index.is_own_trade_tx("0xanything") is False


def test_the_lookup_never_creates_the_ledger(tmp_path):
    trade_index.is_own_trade_tx("0xanything")
    assert not (tmp_path / "home" / "wallet" / "audit.jsonl").exists()


# -- the watcher -----------------------------------------------------------

@pytest.mark.asyncio
async def test_our_own_swap_proceeds_are_skipped_with_a_breadcrumb(
        tmp_path, _emitted):
    """The legitimate case the heuristic exists for: a sell's proceeds arrive
    from the router and correlate to a trade we recorded."""
    _write_audit(tmp_path, [{"venue": "defi", "action": "swap",
                             "result_ref": "0xoursell"}])
    db = await _setup_db(tmp_path)
    try:
        watcher = SettlementWatcher(_WakeAgent(), db=db)
        transfer = {"tx_hash": "0xoursell", "from": ROUTER,
                    "amount_usd": 42.0, "block": 7}
        settled, unmatched = await watcher._settle_or_flag(
            [transfer], TREASURY.lower(), "base")
        assert (settled, unmatched) == (0, 0)
        kinds = [k for k, _ in _emitted]
        assert "payment_unmatched" not in kinds
        assert "payment_self_proceeds" in kinds, (
            "a silent skip is how a real payment disappears — the case must "
            "leave a breadcrumb")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_router_sourced_payment_we_did_not_make_is_still_flagged(
        tmp_path, _emitted):
    """THE BUG: a genuine payer routing through the same public router, or an
    overpayment that misses the exact-amount match, was silently dropped."""
    _write_audit(tmp_path, [{"venue": "defi", "action": "swap",
                             "result_ref": "0xsomeothertrade"}])
    db = await _setup_db(tmp_path)
    try:
        watcher = SettlementWatcher(_WakeAgent(), db=db)
        transfer = {"tx_hash": "0xrealpayment", "from": ROUTER,
                    "amount_usd": 42.0, "block": 7}
        settled, unmatched = await watcher._settle_or_flag(
            [transfer], TREASURY.lower(), "base")
        assert (settled, unmatched) == (0, 1)
        assert "payment_unmatched" in [k for k, _ in _emitted]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_unreadable_ledger_flags_rather_than_swallows(
        tmp_path, _emitted):
    """No ledger at all: nothing can be confirmed as ours, so the transfer
    takes the owner-notice path."""
    db = await _setup_db(tmp_path)
    try:
        watcher = SettlementWatcher(_WakeAgent(), db=db)
        transfer = {"tx_hash": "0xunknown", "from": ROUTER,
                    "amount_usd": 9.0, "block": 3}
        settled, unmatched = await watcher._settle_or_flag(
            [transfer], TREASURY.lower(), "base")
        assert (settled, unmatched) == (0, 1)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_hash_match_is_case_insensitive(tmp_path, _emitted):
    """The ledger's hash comes from web3, the scanner's from `eth_getLogs` —
    different cases for the same transaction."""
    _write_audit(tmp_path, [{"venue": "defi", "action": "swap",
                             "result_ref": "0xABCDEF"}])
    db = await _setup_db(tmp_path)
    try:
        watcher = SettlementWatcher(_WakeAgent(), db=db)
        transfer = {"tx_hash": "0xabcdef", "from": ROUTER,
                    "amount_usd": 1.0, "block": 1}
        settled, unmatched = await watcher._settle_or_flag(
            [transfer], TREASURY.lower(), "base")
        assert (settled, unmatched) == (0, 0)
    finally:
        await db.close()
