"""Task 3 (settlement / invoicing hardening) — H7, M5, M6, M8, M9, M11, H9, L8.

Runs against the REAL x402 schema via DatabaseConnection (the all-fakes pattern
is why N1 shipped). Each test names the finding it locks in.
"""
import asyncio
import time

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing, subscriptions as subs
from modules.x402.settlement_watcher import SettlementWatcher


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_SETTLE_ONCHAIN_DETECT", "X402_INVOICE_AMOUNT_JITTER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PAYMENT_APPROVAL_MODE", raising=False)
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    yield
    _c._refreeze_payment_approval_flags_for_tests()


def _board(tmp_path):
    from agents.task.goals.board import GoalBoard
    return GoalBoard(str(tmp_path / "goals.db"))


class _Agent:
    """Task-agent double: records wakes + correspondent deliveries + owner
    notices; no active correspondent binding by default."""

    def __init__(self):
        self.wakes = []
        self.correspondent_deliveries = []
        self.owner_notices = []

        class _Reg:
            def resolve(self, *, surface, address, thread_id=None):
                return None

        class _Container:
            def __init__(self, reg):
                self._reg = reg

            def get_service(self, name):
                return self._reg if name == "correspondent_registry" else None

        self.container = _Container(_Reg())

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return True

    async def deliver_correspondent_data(self, session_id, source, text, metadata=None):
        self.correspondent_deliveries.append((session_id, source, text, metadata))
        return True


# ---------------------------------------------------------------------------
# M6 — settled renewal while SUBSCRIPTIONS_ENABLED=off
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_m6_settled_renewal_while_subs_off_withholds_wake_and_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "false")
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="p@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess1", amount_usd=10.0,
            purpose="renewal", subscription_id=sub["id"], db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)

        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()

        # NEVER the ordinary "settled, continue" wake.
        assert out["settled_notified"] == 0
        assert agent.wakes == []
        assert agent.correspondent_deliveries == []
        # Retryable: wake_delivered still false -> still in the unnotified set.
        still = await invoicing.settled_unnotified_invoices(db=db)
        assert any(i["request_id"] == inv["request_id"] for i in still)
        # Extension withheld (subs off).
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["paid_through"] == now

        # Re-enable -> the SAME invoice is applied + normally woken.
        monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
        out2 = await watcher.tick_once()
        assert out2["settled_notified"] == 1
        row2 = await subs.get_subscription(sub["id"], db=db)
        assert row2["paid_through"] == now + 30 * 86400
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# M8 — paid_through extension is atomic under concurrent application
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_m8_apply_settlement_extends_atomically_ignoring_stale_read(tmp_path, monkeypatch):
    """Pre-fix, apply_settlement read paid_through into Python BEFORE its
    transaction and blind-wrote base+period — a second applier that read the
    same stale base lost a period. The fix computes COALESCE(paid_through)+period
    IN SQL, so it lands on the DB's CURRENT value even when the Python `sub`
    snapshot is stale (simulated here)."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="p@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)
        # One pending renewal per subscription at a time (the pending-unique
        # index), so create+settle inv1 before minting inv2.
        inv1 = await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=10.0, purpose="r1",
            subscription_id=sub["id"], db=db)
        await invoicing.settle_payment_request(inv1["request_id"], db=db)
        r1 = await subs.apply_settlement(sub["id"], inv1["request_id"], db=db)
        assert r1 == subs.SettlementResult.APPLIED
        assert (await subs.get_subscription(sub["id"], db=db))["paid_through"] == now + 30 * 86400

        inv2 = await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=11.0, purpose="r2",
            subscription_id=sub["id"], db=db)
        await invoicing.settle_payment_request(inv2["request_id"], db=db)

        # Second application, but with a STALE Python snapshot (paid_through=now)
        # as a concurrent applier would have read before the first commit. The
        # atomic SQL UPDATE must still land now + 60d, NOT stale now + 30d.
        real_get = subs.get_subscription

        async def stale_get(subscription_id, *, user_id=None, db=None):
            row = await real_get(subscription_id, user_id=user_id, db=db)
            if row is not None:
                row = dict(row)
                row["paid_through"] = now  # stale!
            return row

        monkeypatch.setattr(subs, "get_subscription", stale_get)
        r2 = await subs.apply_settlement(sub["id"], inv2["request_id"], db=db)
        monkeypatch.setattr(subs, "get_subscription", real_get)
        assert r2 == subs.SettlementResult.APPLIED

        row = await subs.get_subscription(sub["id"], db=db)
        assert row["paid_through"] == now + 60 * 86400  # both periods landed
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# M5 — cross-process same-amount collision closed by a partial UNIQUE index
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_m5_index_retries_jitter_on_cross_process_collision(tmp_path, monkeypatch):
    """workers>1: a concurrent worker's SELECT-then-INSERT can miss the
    in-process dedupe. The partial UNIQUE index is the atomic backstop — the
    INSERT raises IntegrityError and create_payment_request retries the next
    jitter candidate (simulated by neutering the in-process dedupe)."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "true")
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=7.5, purpose="a", db=db)
        assert first["amount_usd"] == 7.5

        async def blind_dedupe(amount_usd, recipient, cap, database):
            return round(float(amount_usd), 6)  # pretend no collision (cross-process race)

        monkeypatch.setattr(invoicing, "_dedupe_amount_for_treasury", blind_dedupe)
        second = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=7.5, purpose="b", db=db)
        assert second["amount_usd"] == 7.5001  # bumped one jitter step by the retry
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_m5_no_index_when_detection_off_allows_legacy_duplicate(tmp_path, monkeypatch):
    """Detection OFF (default): two same-amount pending invoices are still
    INTENTIONALLY allowed (byte-identical legacy) — the index is not created."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "false")
    db = await _setup_db(tmp_path)
    try:
        a = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=3.0, purpose="a", db=db)
        b = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=3.0, purpose="b", db=db)
        assert a["amount_usd"] == 3.0 and b["amount_usd"] == 3.0
        idx = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?",
            (invoicing._PENDING_AMOUNT_INDEX,))
        assert idx == []
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# M11 — renewal grants are amount-bound and TTL-scoped
# ---------------------------------------------------------------------------

async def _make_due_sub(db, *, amount_usd=10.0):
    return await subs.create_subscription(
        user_id="rob", correspondent_surface="email",
        correspondent_address="p@x.com", cron_job_id="job1",
        amount_usd=amount_usd, renewal_lead_days=5, grace_days=3,
        paid_through=int(time.time()) + 2 * 86400, db=db)


@pytest.mark.asyncio
async def test_m11_renewal_grant_refused_on_amount_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    db = await _setup_db(tmp_path)
    board = _board(tmp_path)
    try:
        sub = await _make_due_sub(db, amount_usd=10.0)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=board)
        await watcher.tick_once()  # queues an ask

        from agents.task.goals.board import ASK_OPEN
        ask = board.asks(user_id="rob", status=ASK_OPEN)[0]
        assert ask.payload["amount_usd"] == 10.0  # amount stamped (M11)
        board.decide_ask(ask.id, user_id="rob", approved=True)

        # The subscription's charge changes AFTER approval.
        await db.execute("UPDATE subscriptions SET amount_usd = ? WHERE id = ?",
                         (20.0, sub["id"]))

        out = await watcher.tick_once()
        # Stale-amount grant NOT consumed -> no invoice; a fresh ask at $20 queued.
        assert out["subscription_renewals_invoiced"] == 0
        assert await invoicing.list_payment_requests(user_id="rob", db=db) == []
        open_asks = board.asks(user_id="rob", status=ASK_OPEN)
        assert len(open_asks) == 1
        assert open_asks[0].payload["amount_usd"] == 20.0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_m11_renewal_grant_expired_by_ttl_not_consumed(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    # Force every completed grant to read as expired (TTL check wired, M11).
    # approval_queue reads this lazily from core.config_policy (WS-1 ph3) — patch there.
    import core.config_policy as _policy
    monkeypatch.setattr(_policy, "approval_grant_ttl_hours", lambda: -1.0)
    db = await _setup_db(tmp_path)
    board = _board(tmp_path)
    try:
        await _make_due_sub(db, amount_usd=10.0)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=board)
        await watcher.tick_once()
        from agents.task.goals.board import ASK_OPEN
        ask = board.asks(user_id="rob", status=ASK_OPEN)[0]
        board.decide_ask(ask.id, user_id="rob", approved=True)

        out = await watcher.tick_once()
        # Expired grant -> not consumed -> no invoice minted.
        assert out["subscription_renewals_invoiced"] == 0
        assert await invoicing.list_payment_requests(user_id="rob", db=db) == []
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# H7 — stale-'settling' reaper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h7_stale_settling_reaper_reverts_to_pending(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="svc", db=db)
        # Strand it in 'settling' with an OLD updated_at (>10 min).
        assert await invoicing.claim_for_settlement(inv["request_id"], db=db)
        await db.execute(
            "UPDATE x402_payment_requests SET updated_at = datetime('now', '-20 minutes') "
            "WHERE id = ?", (inv["request_id"],))

        reverted = await invoicing.revert_stale_settling(max_age_seconds=600, db=db)
        assert [r["request_id"] for r in reverted] == [inv["request_id"]]
        row = await invoicing.get_payment_request(inv["request_id"], db=db)
        assert row["status"] == "pending"  # payable again
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_h7_reaper_leaves_recent_settling_untouched(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="svc", db=db)
        assert await invoicing.claim_for_settlement(inv["request_id"], db=db)  # fresh 'settling'
        reverted = await invoicing.revert_stale_settling(max_age_seconds=600, db=db)
        assert reverted == []  # too young to be considered stranded
        row = await invoicing.get_payment_request(inv["request_id"], db=db)
        assert row["status"] == "settling"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_h7_sweep_wired_into_tick(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="svc", db=db)
        await invoicing.claim_for_settlement(inv["request_id"], db=db)
        await db.execute(
            "UPDATE x402_payment_requests SET updated_at = datetime('now', '-20 minutes') "
            "WHERE id = ?", (inv["request_id"],))
        watcher = SettlementWatcher(_Agent(), db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()
        assert out["settling_reverted"] == 1
        row = await invoicing.get_payment_request(inv["request_id"], db=db)
        assert row["status"] == "pending"
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# L8 — kind filter uses json_extract (matches compact-JSON rows)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_l8_kind_filter_matches_compact_metadata(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="svc", db=db)
        # Simulate the boot-time subscription dedup rewriting metadata via
        # SQLite json_set -> COMPACT JSON ("kind":"agent_invoice", no space).
        await db.execute(
            "UPDATE x402_payment_requests SET metadata = json_set(metadata, '$.touched', 1) "
            "WHERE id = ?", (inv["request_id"],))

        chk = await db.fetch_one(
            "SELECT (metadata LIKE '%\"kind\": \"agent_invoice\"%') AS spaced, "
            "json_extract(metadata, '$.kind') AS k "
            "FROM x402_payment_requests WHERE id = ?", (inv["request_id"],))
        assert chk["spaced"] == 0  # the OLD spaced LIKE now MISSES
        assert chk["k"] == "agent_invoice"  # json_extract (L8) still matches

        # The reader (json_extract-based) still finds it.
        rows = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert any(r["request_id"] == inv["request_id"] for r in rows)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# H9 — one atomic-amount helper (round, not truncate)
# ---------------------------------------------------------------------------

def test_h9_atomic_amount_rounds_not_truncates():
    from modules.x402.middleware import to_atomic_amount
    # 1.001 * 1e6 == 1000999.9999999999 -> truncation gives 1000999 (the pre-fix
    # verification path), round gives 1001000 (the challenge). One atomic unit of
    # drift bricked anonymous payment at such price points; the helper rounds.
    assert to_atomic_amount(1.001, 6) == 1001000
    assert int(1.001 * (10 ** 6)) == 1000999  # the old truncating form disagreed
    assert to_atomic_amount(1.001, 6) != int(1.001 * (10 ** 6))


def test_h9_challenge_uses_rounding_helper(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xt")
    monkeypatch.setenv("X402_PRICE_USD", "1.001")
    from modules.x402 import middleware
    ch = middleware.build_x402_challenge("/x", cost_usd=1.001)
    # The challenge and the verification requirement now share to_atomic_amount,
    # so both are 1001000 — never the 1000999 the truncating verify used to want.
    assert ch["accepts"][0]["maxAmountRequired"] == "1001000"
