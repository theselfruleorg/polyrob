"""Task 11 (Phase 2) — settlement watcher on-chain detection integration:
scan -> match -> claim -> settle -> the EXISTING settled-unnotified wake
path, all the way through `SettlementWatcher.tick_once`. A new file (rather
than extending test_settlement_watcher.py) to keep this shared-tree-heavy
module's existing test file untouched."""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing, onchain_probe
from modules.x402.settlement_watcher import SettlementWatcher

TREASURY = "0xTreasuryAddress000000000000000000000001"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    # Isolate scan/match behavior from the jitter feature (covered separately
    # in test_onchain_settlement.py) — both same-amount tests below want the
    # exact, unnudged amount.
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "false")
    for var in ("X402_SETTLE_ONCHAIN_DETECT", "X402_SETTLEMENT_SCAN_MAX_SPAN",
                "X402_SETTLEMENT_CONFIRMATIONS"):
        monkeypatch.delenv(var, raising=False)


def _pad(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def _log(tx_hash, from_addr, value_atomic, block, to_addr=TREASURY):
    return {
        "address": USDC,
        "topics": [onchain_probe.TRANSFER_TOPIC, _pad(from_addr), _pad(to_addr)],
        "data": hex(value_atomic),
        "blockNumber": hex(block),
        "transactionHash": tx_hash,
    }


class _FakeChain:
    """A tiny fake RPC surface: head block + a log set, range-aware for
    `eth_getLogs` so checkpoint semantics are exercised for real (not just
    "always return everything")."""

    def __init__(self, head=1000):
        self.head = head
        self.logs = []
        self.get_logs_calls = 0

    def rpc(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getLogs":
            self.get_logs_calls += 1
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            return [log for log in self.logs if lo <= int(log["blockNumber"], 16) <= hi]
        raise AssertionError(f"unexpected RPC method {method}")


class _WakeAgent:
    def __init__(self, result=True):
        self.result = result
        self.wakes = []

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return self.result


@pytest.mark.asyncio
async def test_detection_off_by_default_no_scan_no_tables_touched(tmp_path, monkeypatch):
    monkeypatch.delenv("X402_SETTLE_ONCHAIN_DETECT", raising=False)
    db = await _setup_db(tmp_path)
    try:
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=12.34, purpose="p", db=db)
        chain = _FakeChain()
        chain.logs = [_log("0xpay", "0xPayer0000000000000000000000000000000001",
                            12_340000, 500)]
        out = await SettlementWatcher(
            _WakeAgent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC).tick_once()
        assert out["onchain_settled"] == 0
        assert out["onchain_unmatched"] == 0
        assert chain.get_logs_calls == 0  # never even scanned
        assert await invoicing.get_scan_checkpoint(TREASURY.lower(), db=db) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_detection_on_but_testnet_chain_no_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base-sepolia")
    db = await _setup_db(tmp_path)
    try:
        chain = _FakeChain()
        out = await SettlementWatcher(
            _WakeAgent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC).tick_once()
        assert out["onchain_settled"] == 0
        assert chain.get_logs_calls == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_detection_on_no_treasury_configured_no_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "")
    db = await _setup_db(tmp_path)
    try:
        chain = _FakeChain()
        out = await SettlementWatcher(
            _WakeAgent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC).tick_once()
        assert out["onchain_settled"] == 0
        assert chain.get_logs_calls == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_first_tick_seeds_checkpoint_and_scans_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    db = await _setup_db(tmp_path)
    try:
        chain = _FakeChain(head=1000)
        # A transfer that ALREADY exists on-chain before the watcher ever
        # ran — the first tick must never scan genesis to find it.
        chain.logs = [_log("0xold", "0xPayer0000000000000000000000000000000002",
                            1_000000, 100)]
        out = await SettlementWatcher(
            _WakeAgent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC).tick_once()
        assert out["onchain_settled"] == 0
        assert chain.get_logs_calls == 0  # seed-only tick, no eth_getLogs call at all
        checkpoint = await invoicing.get_scan_checkpoint(TREASURY.lower(), db=db)
        assert checkpoint is not None and checkpoint > 100  # seeded near head, not at 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_matched_transfer_settles_and_wakes_same_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_oc", amount_usd=12.34, purpose="widget", db=db)

        chain = _FakeChain(head=1000)
        agent = _WakeAgent()
        watcher = SettlementWatcher(agent, db=db, rpc_call=chain.rpc, usdc_addr=USDC)
        await watcher.tick_once()  # seed only

        chain.head = 1010
        chain.logs = [_log("0xpay1", "0xPayer0000000000000000000000000000000003",
                            12_340000, 1005)]
        out = await watcher.tick_once()

        assert out["onchain_settled"] == 1
        assert out["onchain_unmatched"] == 0
        assert out["settled_notified"] == 1  # SAME-tick wake, via the EXISTING path
        assert len(agent.wakes) == 1
        sid, uid, text, meta = agent.wakes[0]
        assert sid == "sess_oc" and uid == "rob"
        assert "SETTLED" in text and "0xpay1" in text
        assert meta["kind_hint"] == "payment_settled"

        row = await db.fetch_one(
            "SELECT status, transaction_hash FROM x402_payment_requests WHERE id = ?",
            (inv["request_id"],))
        assert row["status"] == "completed"
        assert row["transaction_hash"] == "0xpay1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_equal_amount_pending_oldest_settled_newer_stays_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    db = await _setup_db(tmp_path)
    try:
        older = await invoicing.create_payment_request(
            user_id="rob", session_id="s_old", amount_usd=5.0, purpose="a", db=db)
        newer = await invoicing.create_payment_request(
            user_id="rob", session_id="s_new", amount_usd=5.0, purpose="b", db=db)

        chain = _FakeChain(head=1000)
        watcher = SettlementWatcher(_WakeAgent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC)
        await watcher.tick_once()  # seed

        chain.head = 1010
        chain.logs = [_log("0xpay2", "0xPayer0000000000000000000000000000000004",
                            5_000000, 1005)]
        out = await watcher.tick_once()
        assert out["onchain_settled"] == 1

        old_row = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?", (older["request_id"],))
        new_row = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?", (newer["request_id"],))
        assert old_row["status"] == "completed"
        assert new_row["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unmatched_transfer_emits_event_and_settles_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    events = []
    monkeypatch.setattr(
        "agents.task.telemetry.event_log.event_log_enabled", lambda: True)

    class _FakeLog:
        def record(self, kind, **kw):
            events.append((kind, kw))

    monkeypatch.setattr(
        "agents.task.telemetry.event_log.get_event_log", lambda: _FakeLog())

    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=9.99, purpose="p", db=db)

        chain = _FakeChain(head=1000)
        watcher = SettlementWatcher(_WakeAgent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC)
        await watcher.tick_once()  # seed

        chain.head = 1010
        # amount doesn't match the pending invoice at all
        chain.logs = [_log("0xstray", "0xPayer0000000000000000000000000000000005",
                            1_230000, 1005)]
        out = await watcher.tick_once()

        assert out["onchain_settled"] == 0
        assert out["onchain_unmatched"] == 1
        assert any(kind == "payment_unmatched" for kind, _ in events)
        unmatched_kw = next(kw for kind, kw in events if kind == "payment_unmatched")
        assert unmatched_kw["attrs"]["tx_hash"] == "0xstray"

        row = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?", (inv["request_id"],))
        assert row["status"] == "pending"  # untouched — never settled on no-match
    finally:
        await db.close()


# --- C2: replayed tx_hash + mid-batch failure isolation ---------------------

@pytest.mark.asyncio
async def test_replayed_tx_hash_never_settles_a_different_invoice(tmp_path, monkeypatch):
    """A given on-chain tx settles AT MOST ONE invoice EVER. Simulates the
    real bug scenario directly at the `_settle_or_flag` layer: invoice A was
    already settled by tx 0xreplay; invoice B is a DIFFERENT, still-pending
    invoice at the SAME amount (the exact ambiguity a re-scanned/consumed
    transfer must never be allowed to resolve into)."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    db = await _setup_db(tmp_path)
    try:
        inv_a = await invoicing.create_payment_request(
            user_id="rob", session_id="s_a", amount_usd=5.0, purpose="a", db=db)
        await invoicing.settle_payment_request(
            inv_a["request_id"], transaction_hash="0xreplay", db=db)

        inv_b = await invoicing.create_payment_request(
            user_id="rob", session_id="s_b", amount_usd=5.0, purpose="b", db=db)

        watcher = SettlementWatcher(_WakeAgent(), db=db)
        transfer = {"tx_hash": "0xreplay", "from": "0xPayerReplay",
                    "amount_usd": 5.0, "block": 999}
        settled, unmatched = await watcher._settle_or_flag([transfer], TREASURY.lower())

        assert settled == 0
        assert unmatched == 0  # a replay is neither a new settlement nor a new unmatched
        row_b = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?",
            (inv_b["request_id"],))
        assert row_b["status"] == "pending"  # untouched — never settled by a consumed tx
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settle_payment_request_refuses_a_reused_tx_hash_directly(tmp_path, monkeypatch):
    """Same invariant, exercised directly at the invoicing layer (not via the
    watcher loop): `settle_payment_request` itself must refuse a tx_hash that
    already settled a different invoice."""
    db = await _setup_db(tmp_path)
    try:
        inv_a = await invoicing.create_payment_request(
            user_id="rob", session_id="s_a", amount_usd=5.0, purpose="a", db=db)
        assert await invoicing.settle_payment_request(
            inv_a["request_id"], transaction_hash="0xdupe", db=db) is True

        inv_b = await invoicing.create_payment_request(
            user_id="rob", session_id="s_b", amount_usd=5.0, purpose="b", db=db)
        assert await invoicing.settle_payment_request(
            inv_b["request_id"], transaction_hash="0xdupe", db=db) is False

        row_b = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?",
            (inv_b["request_id"],))
        assert row_b["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_one_transfer_failure_does_not_block_others_in_the_batch(tmp_path, monkeypatch):
    """A mid-batch exception in ONE transfer's match/claim/settle must not
    prevent OTHER transfers in the same tick from settling correctly."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    db = await _setup_db(tmp_path)
    try:
        good = await invoicing.create_payment_request(
            user_id="rob", session_id="s_good", amount_usd=7.0, purpose="good", db=db)
        bad = await invoicing.create_payment_request(
            user_id="rob", session_id="s_bad", amount_usd=8.0, purpose="bad", db=db)

        real_claim = invoicing.claim_for_settlement

        async def flaky_claim(request_id, *, db=None):
            if request_id == bad["request_id"]:
                raise RuntimeError("simulated DB hiccup mid-batch")
            return await real_claim(request_id, db=db)

        monkeypatch.setattr(invoicing, "claim_for_settlement", flaky_claim)

        watcher = SettlementWatcher(_WakeAgent(), db=db)
        transfers = [
            {"tx_hash": "0xbad", "from": "0xP1", "amount_usd": 8.0, "block": 1},
            {"tx_hash": "0xgood", "from": "0xP2", "amount_usd": 7.0, "block": 2},
        ]
        settled, unmatched = await watcher._settle_or_flag(transfers, TREASURY.lower())

        # the failing transfer must not block the good one
        assert settled == 1
        row_good = await db.fetch_one(
            "SELECT status, transaction_hash FROM x402_payment_requests WHERE id = ?",
            (good["request_id"],))
        assert row_good["status"] == "completed"
        assert row_good["transaction_hash"] == "0xgood"
        # the bad invoice is left pending, not corrupted — the claim never landed
        row_bad = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?", (bad["request_id"],))
        assert row_bad["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_checkpoint_advances_even_when_a_transfer_errors(tmp_path, monkeypatch):
    """The scan checkpoint must advance past a fully-processed block range
    even when one of the transfers in it raised — otherwise a permanently-
    failing transfer would wedge the watcher into rescanning the same range
    forever (and would eventually risk the exact replay this fix closes)."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    db = await _setup_db(tmp_path)
    try:
        chain = _FakeChain(head=1000)
        watcher = SettlementWatcher(_WakeAgent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC)
        await watcher.tick_once()  # seed

        chain.head = 1010
        chain.logs = [_log("0xboom", "0xPayer0000000000000000000000000000000009",
                            3_000000, 1005)]

        async def boom(*a, **kw):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(invoicing, "match_pending_invoice_by_amount", boom)

        out = await watcher.tick_once()
        assert out["onchain_settled"] == 0
        assert out["onchain_unmatched"] == 0

        checkpoint = await invoicing.get_scan_checkpoint(TREASURY.lower(), db=db)
        assert checkpoint is not None and checkpoint >= 1005  # advanced despite the error
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_checkpoint_resumes_from_stored_block_not_rescanned(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    db = await _setup_db(tmp_path)
    try:
        chain = _FakeChain(head=1000)
        watcher = SettlementWatcher(_WakeAgent(), db=db, rpc_call=chain.rpc, usdc_addr=USDC)
        await watcher.tick_once()  # seed near head
        seeded = await invoicing.get_scan_checkpoint(TREASURY.lower(), db=db)
        assert seeded is not None

        chain.head = 1005
        await watcher.tick_once()  # scans (seeded+1 .. head-confirmations)
        after = await invoicing.get_scan_checkpoint(TREASURY.lower(), db=db)
        assert after is not None and after > seeded

        # a THIRD tick with no new head movement must not rescan
        calls_before = chain.get_logs_calls
        await watcher.tick_once()
        assert chain.get_logs_calls == calls_before
    finally:
        await db.close()
