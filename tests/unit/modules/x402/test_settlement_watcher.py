"""Money loop — settlement watcher: settle → wake the originating session once."""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing
from modules.x402.settlement_watcher import SettlementWatcher, build_settlement_watcher


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")


class _WakeAgent:
    def __init__(self, result=True):
        self.result = result
        self.wakes = []

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return self.result


@pytest.mark.asyncio
async def test_settled_invoice_wakes_session_exactly_once(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_7", amount_usd=4.0,
            purpose="consulting", db=db)
        await invoicing.settle_payment_request(inv["request_id"],
                                               transaction_hash="0xfeed", db=db)
        agent = _WakeAgent()
        watcher = SettlementWatcher(agent, db=db)
        out = await watcher.tick_once()
        assert out["settled_notified"] == 1
        assert len(agent.wakes) == 1
        sid, uid, text, meta = agent.wakes[0]
        assert sid == "sess_7" and uid == "rob"
        assert inv["request_id"] in text and "SETTLED" in text
        assert meta["kind_hint"] == "payment_settled"
        # second tick: nothing to notify
        out = await watcher.tick_once()
        assert out["settled_notified"] == 0
        assert len(agent.wakes) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dropped_wake_still_marks_notified(tmp_path):
    # SELF_WAKE off / non-resident session -> deliver returns False; the row is
    # still marked so the watcher doesn't retry forever (ledger is the record).
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="gone", amount_usd=1.0, purpose="p", db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        agent = _WakeAgent(result=False)
        out = await SettlementWatcher(agent, db=db).tick_once()
        assert out["settled_notified"] == 1
        assert await invoicing.settled_unnotified_invoices(db=db) == []
    finally:
        await db.close()


class _Registry:
    def __init__(self, state="active"):
        self._state = state

    def resolve(self, *, surface, address, thread_id=None):
        return {"state": self._state} if self._state else None


class _Container:
    def __init__(self, reg):
        self._reg = reg

    def get_service(self, name):
        return self._reg if name == "correspondent_registry" else None


class _CorrAgent:
    """Agent exposing both rails + a container with an (optionally active) registry."""
    def __init__(self, corr_state="active", corr_result=True):
        self.wakes = []
        self.correspondent_deliveries = []
        self.container = _Container(_Registry(corr_state))
        self._corr_result = corr_result

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return True

    async def deliver_correspondent_data(self, session_id, source, text, metadata=None):
        self.correspondent_deliveries.append((session_id, source, text, metadata))
        return self._corr_result


@pytest.mark.asyncio
async def test_correspondent_linked_settlement_delivers_as_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_c", amount_usd=3.0, purpose="svc", db=db,
            correspondent_ref={"surface": "email", "address": "x@y.z", "thread_id": ""})
        await invoicing.settle_payment_request(inv["request_id"],
                                               transaction_hash="0xabc", db=db)
        agent = _CorrAgent(corr_state="active")
        out = await SettlementWatcher(agent, db=db).tick_once()
        assert out["settled_notified"] == 1
        assert len(agent.correspondent_deliveries) == 1
        assert not agent.wakes  # NOT the owner rail
        sid, src, text, meta = agent.correspondent_deliveries[0]
        assert sid == "sess_c" and "email:x@y.z" == src
        assert "SETTLED" in text
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_correspondent_inactive_falls_back_to_self_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_c", amount_usd=3.0, purpose="svc", db=db,
            correspondent_ref={"surface": "email", "address": "x@y.z"})
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        agent = _CorrAgent(corr_state=None)  # registry resolves to no active row
        await SettlementWatcher(agent, db=db).tick_once()
        assert not agent.correspondent_deliveries
        assert len(agent.wakes) == 1  # owner self-wake fallback
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_correspondent_access_disabled_uses_self_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "false")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_c", amount_usd=3.0, purpose="svc", db=db,
            correspondent_ref={"surface": "email", "address": "x@y.z"})
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        agent = _CorrAgent(corr_state="active")
        await SettlementWatcher(agent, db=db).tick_once()
        assert not agent.correspondent_deliveries
        assert len(agent.wakes) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_tick_expires_stale_invoices(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=1.0, purpose="p",
            expiry_hours=0.1, db=db)
        import time as _time
        real_time = _time.time
        monkeypatch.setattr(_time, "time", lambda: real_time() + 3600)
        out = await SettlementWatcher(_WakeAgent(), db=db).tick_once()
        assert out["expired"] == 1
    finally:
        await db.close()


def test_builder_reads_interval_env(monkeypatch):
    monkeypatch.setenv("X402_SETTLEMENT_WATCH_INTERVAL_SEC", "120")
    w = build_settlement_watcher(object())
    assert w.interval_seconds == 120


# --- G-22: non-payment escalation --------------------------------------------
# An invoice that lapses unpaid must wake the originating session (or the
# owner) instead of silently vanishing.

def _expire_soon(monkeypatch):
    """Advance time.time() by 1h so a 0.1h-expiry invoice is stale."""
    import time as _time
    real_time = _time.time
    monkeypatch.setattr(_time, "time", lambda: real_time() + 3600)


def _capture_owner_notifications(monkeypatch):
    calls = []

    async def _fake_deliver(container, user_id, text, **kwargs):
        calls.append({"container": container, "user_id": user_id, "text": text,
                      "kwargs": kwargs})
        return "sent"

    monkeypatch.setattr(
        "core.surfaces.user_delivery.deliver_user_message", _fake_deliver)
    return calls


@pytest.mark.asyncio
async def test_expired_correspondent_active_delivers_data_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    owner_calls = _capture_owner_notifications(monkeypatch)
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_exp_c", amount_usd=2.0, purpose="svc",
            expiry_hours=0.1, db=db,
            correspondent_ref={"surface": "email", "address": "x@y.z", "thread_id": ""})
        _expire_soon(monkeypatch)
        agent = _CorrAgent(corr_state="active")
        watcher = SettlementWatcher(agent, db=db)
        out = await watcher.tick_once()
        assert out["expired"] == 1
        assert out["expired_notified"] == 1
        assert len(agent.correspondent_deliveries) == 1
        assert not agent.wakes  # NOT the owner self-wake rail
        sid, src, text, meta = agent.correspondent_deliveries[0]
        assert sid == "sess_exp_c" and src == "email:x@y.z"
        assert "EXPIRED" in text and inv["request_id"] in text
        assert meta["kind_hint"] == "payment_expired"
        # the owner ALSO gets a durable notification, regardless of the
        # session-side correspondent delivery
        assert len(owner_calls) == 1
        assert owner_calls[0]["user_id"] == "rob"
        assert inv["request_id"] in owner_calls[0]["text"]

        # second tick: exactly-once claim — no re-delivery on either rail
        out2 = await watcher.tick_once()
        assert out2["expired_notified"] == 0
        assert len(agent.correspondent_deliveries) == 1
        assert len(owner_calls) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expired_no_correspondent_self_wake_and_owner_notice(tmp_path, monkeypatch):
    owner_calls = _capture_owner_notifications(monkeypatch)
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_exp_w", amount_usd=3.5, purpose="report",
            expiry_hours=0.1, db=db)
        _expire_soon(monkeypatch)
        agent = _WakeAgent()
        out = await SettlementWatcher(agent, db=db).tick_once()
        assert out["expired"] == 1
        assert out["expired_notified"] == 1
        assert len(agent.wakes) == 1
        sid, uid, text, meta = agent.wakes[0]
        assert sid == "sess_exp_w" and uid == "rob"
        assert "EXPIRED" in text and inv["request_id"] in text
        assert meta["kind_hint"] == "payment_expired"
        # plus exactly one owner notification over the delivery rail
        assert len(owner_calls) == 1
        assert owner_calls[0]["user_id"] == "rob"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expired_self_wake_disabled_still_marks_and_notifies_owner(tmp_path, monkeypatch):
    owner_calls = _capture_owner_notifications(monkeypatch)
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="gone", amount_usd=1.0, purpose="p",
            expiry_hours=0.1, db=db)
        _expire_soon(monkeypatch)
        agent = _WakeAgent(result=False)  # SELF_WAKE off / non-resident -> dropped
        out = await SettlementWatcher(agent, db=db).tick_once()
        assert out["expired_notified"] == 1
        # wake was attempted (and dropped) but the row is marked exactly once
        assert len(agent.wakes) == 1
        assert await invoicing.expired_unnotified_invoices(db=db) == []
        # the owner notification still fires despite the dropped wake
        assert len(owner_calls) == 1
        assert inv["request_id"] in owner_calls[0]["text"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settled_row_not_expiry_woken_and_expired_row_not_settlement_woken(
        tmp_path, monkeypatch):
    _capture_owner_notifications(monkeypatch)
    db = await _setup_db(tmp_path)
    try:
        settled_inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s_settled", amount_usd=1.0, purpose="a", db=db)
        expired_inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s_expired", amount_usd=1.0, purpose="b",
            expiry_hours=0.1, db=db)
        await invoicing.settle_payment_request(settled_inv["request_id"], db=db)
        _expire_soon(monkeypatch)

        agent = _WakeAgent()
        out = await SettlementWatcher(agent, db=db).tick_once()
        assert out["settled_notified"] == 1
        assert out["expired_notified"] == 1
        assert len(agent.wakes) == 2
        by_kind = {w[3]["kind_hint"]: w for w in agent.wakes}
        assert set(by_kind.keys()) == {"payment_settled", "payment_expired"}
        assert by_kind["payment_settled"][0] == "s_settled"
        assert by_kind["payment_expired"][0] == "s_expired"
        # no double terminal wake: exactly one wake per row, no cross-fire
        assert sum(1 for w in agent.wakes if w[0] == "s_settled") == 1
        assert sum(1 for w in agent.wakes if w[0] == "s_expired") == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_tick_with_no_expired_rows_is_noop(tmp_path, monkeypatch):
    owner_calls = _capture_owner_notifications(monkeypatch)
    db = await _setup_db(tmp_path)
    try:
        # a pending, not-yet-expired invoice must not trigger expiry escalation
        await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=1.0, purpose="p",
            expiry_hours=10.0, db=db)
        agent = _WakeAgent()
        out = await SettlementWatcher(agent, db=db).tick_once()
        assert out["expired"] == 0
        assert out["expired_notified"] == 0
        assert out["settled_notified"] == 0
        assert not agent.wakes
        assert not owner_calls
    finally:
        await db.close()
