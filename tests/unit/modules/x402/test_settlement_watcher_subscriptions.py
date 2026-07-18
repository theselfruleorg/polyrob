"""Watchtower subscription renewal/lapse mechanics on the settlement-watcher
tick (Task 14, Phase 3 R5) — driven entirely from the EXISTING tick, gated
`SUBSCRIPTIONS_ENABLED` (default OFF, byte-identical when off)."""
import time

import pytest

from agents.task.goals.board import GoalBoard
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
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.delenv("PAYMENT_APPROVAL_MODE", raising=False)
    # PAYMENT_APPROVAL_MODE is frozen at import — re-snapshot per test so
    # monkeypatch.setenv actually takes effect (see agents/task/constants.py).
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    yield
    _c._refreeze_payment_approval_flags_for_tests()


def _board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


class _Registry:
    """Correspondent registry double resolving to an ACTIVE session_id."""

    def __init__(self, session_id="corr_sess", state="active"):
        self._session_id = session_id
        self._state = state

    def resolve(self, *, surface, address, thread_id=None):
        if not self._state:
            return None
        return {"state": self._state, "session_id": self._session_id}


class _Container:
    def __init__(self, reg):
        self._reg = reg

    def get_service(self, name):
        return self._reg if name == "correspondent_registry" else None


class _Agent:
    """Task-agent double exposing container + correspondent delivery."""

    def __init__(self, reg=None):
        self.container = _Container(reg or _Registry())
        self.correspondent_deliveries = []
        self.wakes = []

    async def deliver_correspondent_data(self, session_id, source, text, metadata=None):
        self.correspondent_deliveries.append((session_id, source, text, metadata))
        return True

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return True


async def _make_sub(db, *, paid_through_offset_days=2, lead_days=5, grace_days=3,
                    amount_usd=10.0, user_id="rob"):
    return await subs.create_subscription(
        user_id=user_id, correspondent_surface="email",
        correspondent_address="payer@example.com", cron_job_id="job1",
        amount_usd=amount_usd, renewal_lead_days=lead_days, grace_days=grace_days,
        paid_through=int(time.time()) + paid_through_offset_days * 86400, db=db)


# --- flag off: byte-identical -------------------------------------------------

@pytest.mark.asyncio
async def test_flag_off_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "false")
    db = await _setup_db(tmp_path)
    try:
        sub = await _make_sub(db, paid_through_offset_days=-100)  # deeply lapsed
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()
        assert out["subscription_renewals_invoiced"] == 0
        assert out["subscription_grace"] == 0
        assert out["subscription_suspended"] == 0
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["status"] == subs.STATUS_ACTIVE  # untouched
        assert not agent.correspondent_deliveries
    finally:
        await db.close()


# --- renewal invoice creation (auto mode) ------------------------------------

@pytest.mark.asyncio
async def test_auto_mode_creates_and_delivers_renewal_invoice(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    db = await _setup_db(tmp_path)
    try:
        sub = await _make_sub(db, paid_through_offset_days=2, lead_days=5)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()
        assert out["subscription_renewals_invoiced"] == 1

        rows = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"

        # Delivered to the correspondent (an active binding resolved).
        assert len(agent.correspondent_deliveries) == 1
        sid, src, text, meta = agent.correspondent_deliveries[0]
        assert sid == "corr_sess" and src == "email:payer@example.com"
        assert meta["kind_hint"] == "subscription_renewal_invoiced"

        # Second tick: an OPEN pending invoice already exists -> not re-created.
        out2 = await watcher.tick_once()
        assert out2["subscription_renewals_invoiced"] == 0
        rows2 = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert len(rows2) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_mode_no_correspondent_binding_still_creates_invoice(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    db = await _setup_db(tmp_path)
    try:
        sub = await _make_sub(db, paid_through_offset_days=2, lead_days=5)
        agent = _Agent(reg=_Registry(state=None))  # no active binding
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()
        assert out["subscription_renewals_invoiced"] == 1
        assert not agent.correspondent_deliveries  # nothing to route to — skipped, not failed
        rows = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert len(rows) == 1
    finally:
        await db.close()


# --- renewal invoice creation (approve mode) ---------------------------------

@pytest.mark.asyncio
async def test_approve_mode_queues_ask_not_invoice(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    db = await _setup_db(tmp_path)
    board = _board(tmp_path)
    try:
        sub = await _make_sub(db, paid_through_offset_days=2, lead_days=5)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=board)
        out = await watcher.tick_once()
        assert out["subscription_renewals_invoiced"] == 0

        rows = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert rows == []  # no money moved yet

        from agents.task.goals.board import ASK_OPEN
        open_asks = board.asks(user_id="rob", status=ASK_OPEN)
        assert len(open_asks) == 1
        payload = open_asks[0].payload
        assert payload["ask_kind"] == "tool_approval"
        assert payload["tool_name"] == "subscription_renewal"
        assert payload["subscription_id"] == sub["id"]

        # Second tick: the SAME open ask -> no duplicate ask, still no invoice.
        out2 = await watcher.tick_once()
        assert out2["subscription_renewals_invoiced"] == 0
        assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_approve_mode_approved_ask_invoices_on_next_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    db = await _setup_db(tmp_path)
    board = _board(tmp_path)
    try:
        sub = await _make_sub(db, paid_through_offset_days=2, lead_days=5)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=board)
        await watcher.tick_once()

        from agents.task.goals.board import ASK_OPEN
        ask = board.asks(user_id="rob", status=ASK_OPEN)[0]
        ok, _ = board.decide_ask(ask.id, user_id="rob", approved=True)
        assert ok is True

        out2 = await watcher.tick_once()
        assert out2["subscription_renewals_invoiced"] == 1
        rows = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert len(rows) == 1

        # A THIRD tick must not double-invoice (grant already consumed, and a
        # pending invoice now exists so the sub isn't even "needing renewal").
        out3 = await watcher.tick_once()
        assert out3["subscription_renewals_invoiced"] == 0
        rows3 = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert len(rows3) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_approve_mode_rejected_ask_backs_off(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    db = await _setup_db(tmp_path)
    board = _board(tmp_path)
    try:
        sub = await _make_sub(db, paid_through_offset_days=2, lead_days=5)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=board)
        await watcher.tick_once()

        from agents.task.goals.board import ASK_OPEN
        ask = board.asks(user_id="rob", status=ASK_OPEN)[0]
        ok, _ = board.decide_ask(ask.id, user_id="rob", approved=False)
        assert ok is True

        # Immediately after rejection: no new ask spammed, no invoice.
        out2 = await watcher.tick_once()
        assert out2["subscription_renewals_invoiced"] == 0
        assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 0
        rows = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert rows == []
    finally:
        await db.close()


# --- H5: owner kill-switch halts auto-mode renewal minting -------------------

@pytest.mark.asyncio
async def test_auto_mode_renewal_skipped_while_halted_and_retries_after_resume(
        tmp_path, monkeypatch):
    """H5 (renewals leg): while the owner kill-switch is HALTED the auto-mode
    renewal must not mint an invoice; it must SKIP (no state burned) and resume
    minting on the next tick once the owner resumes."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    db = await _setup_db(tmp_path)
    try:
        await _make_sub(db, paid_through_offset_days=2, lead_days=5)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))

        # HALTED: no invoice minted.
        monkeypatch.setattr("agents.task.constants.AutonomyConfig.autonomy_halted",
                            lambda: True)
        out1 = await watcher.tick_once()
        assert out1["subscription_renewals_invoiced"] == 0
        assert await invoicing.list_payment_requests(user_id="rob", db=db) == []

        # RESUMED: the same due subscription now mints normally.
        monkeypatch.setattr("agents.task.constants.AutonomyConfig.autonomy_halted",
                            lambda: False)
        out2 = await watcher.tick_once()
        assert out2["subscription_renewals_invoiced"] == 1
        assert len(await invoicing.list_payment_requests(user_id="rob", db=db)) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_renewal_fails_closed_when_halt_probe_raises(tmp_path, monkeypatch):
    """A halt probe error must SKIP the renewal (fail closed), not mint."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    db = await _setup_db(tmp_path)
    try:
        await _make_sub(db, paid_through_offset_days=2, lead_days=5)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))

        def _boom():
            raise RuntimeError("halt probe blew up")
        monkeypatch.setattr("agents.task.constants.AutonomyConfig.autonomy_halted", _boom)
        out = await watcher.tick_once()
        assert out["subscription_renewals_invoiced"] == 0
        assert await invoicing.list_payment_requests(user_id="rob", db=db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_approve_mode_halt_does_not_burn_approved_grant(tmp_path, monkeypatch):
    """H5: skipping a renewal while halted must NOT consume a one-shot owner grant.
    An owner-APPROVED renewal held during a halt is minted intact after resume."""
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    from agents.task import constants as _c
    _c._refreeze_payment_approval_flags_for_tests()
    db = await _setup_db(tmp_path)
    board = _board(tmp_path)
    try:
        await _make_sub(db, paid_through_offset_days=2, lead_days=5)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=board)

        # Tick 1 queues the ask; the owner approves it.
        await watcher.tick_once()
        from agents.task.goals.board import ASK_OPEN
        ask = board.asks(user_id="rob", status=ASK_OPEN)[0]
        ok, _ = board.decide_ask(ask.id, user_id="rob", approved=True)
        assert ok is True

        # HALTED: the approved grant is held, NOT consumed, no invoice.
        monkeypatch.setattr("agents.task.constants.AutonomyConfig.autonomy_halted",
                            lambda: True)
        out_halted = await watcher.tick_once()
        assert out_halted["subscription_renewals_invoiced"] == 0
        assert await invoicing.list_payment_requests(user_id="rob", db=db) == []

        # RESUMED: the SAME grant is still consumable -> the invoice mints once.
        monkeypatch.setattr("agents.task.constants.AutonomyConfig.autonomy_halted",
                            lambda: False)
        out_resumed = await watcher.tick_once()
        assert out_resumed["subscription_renewals_invoiced"] == 1
        assert len(await invoicing.list_payment_requests(user_id="rob", db=db)) == 1
    finally:
        await db.close()


# --- settlement applies to the subscription (idempotent) ---------------------

@pytest.mark.asyncio
async def test_settled_renewal_invoice_extends_subscription(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="p@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=10.0, purpose="renewal",
            subscription_id=sub["id"], db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)

        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()
        assert out["settled_notified"] == 1

        row = await subs.get_subscription(sub["id"], db=db)
        assert row["paid_through"] == now + 30 * 86400

        # A second tick has nothing new to settle/apply (already notified,
        # and apply_settlement is keyed on request_id so it wouldn't
        # double-extend even if re-invoked).
        out2 = await watcher.tick_once()
        assert out2["settled_notified"] == 0
        row2 = await subs.get_subscription(sub["id"], db=db)
        assert row2["paid_through"] == now + 30 * 86400
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settled_non_subscription_invoice_unaffected(tmp_path):
    """A plain (non-subscription) invoice settling must not touch the
    subscriptions table at all."""
    db = await _setup_db(tmp_path)
    try:
        sub = await _make_sub(db, paid_through_offset_days=100)  # not due
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=5.0, purpose="one-off", db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)

        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        await watcher.tick_once()

        row = await subs.get_subscription(sub["id"], db=db)
        assert row["status"] == subs.STATUS_ACTIVE
        assert row["paid_through"] == sub["paid_through"]
    finally:
        await db.close()


# --- grace / suspend lifecycle ------------------------------------------------

@pytest.mark.asyncio
async def test_lapsed_subscription_moves_to_grace(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        sub = await _make_sub(db, paid_through_offset_days=-1, grace_days=3)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()
        assert out["subscription_grace"] == 1
        assert out["subscription_suspended"] == 0
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["status"] == subs.STATUS_GRACE
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_deeply_lapsed_subscription_suspends_with_notices(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        sub = await _make_sub(db, paid_through_offset_days=-10, grace_days=3)
        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()
        assert out["subscription_suspended"] == 1
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["status"] == subs.STATUS_SUSPENDED

        # One correspondent notice.
        assert len(agent.correspondent_deliveries) == 1
        sid, src, text, meta = agent.correspondent_deliveries[0]
        assert meta["kind_hint"] == "subscription_suspended"

        # Second tick: already suspended -> no re-notify.
        out2 = await watcher.tick_once()
        assert out2["subscription_suspended"] == 0
        assert len(agent.correspondent_deliveries) == 1
    finally:
        await db.close()


# --- Task 14 review Finding 1: a failed apply_settlement must not lose the --
# --- wake / permanently strand the renewal extension -------------------------

@pytest.mark.asyncio
async def test_apply_settlement_failure_does_not_deliver_wake_and_retries_next_tick(
        tmp_path, monkeypatch):
    """If `subscriptions.apply_settlement` raises (e.g. a transient DB error)
    while processing a settled renewal invoice, the settlement watcher must
    NOT fall through to claim_wake/_notify for that invoice this tick — doing
    so would mark metadata.wake_delivered=true, and
    settled_unnotified_invoices only ever returns wake_delivered=false rows,
    so the row would never be retried again and the paid renewal's
    paid_through extension would be silently, permanently lost.

    Without the fix (a bare `except Exception: logger.warning(...)` with no
    `continue`), this test fails: tick 1 would show settled_notified == 1 (a
    wake WAS delivered) even though the extension never applied — proving the
    lost-renewal bug. With the fix, tick 1 shows settled_notified == 0 and no
    wake at all; the SAME invoice is retried on tick 2, the extension applies,
    and the wake fires exactly once (idempotency holds across the retry)."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="p@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_retry", amount_usd=10.0,
            purpose="renewal", subscription_id=sub["id"], db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)

        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))

        real_apply = subs.apply_settlement
        calls = {"n": 0}

        async def flaky_apply(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("injected apply_settlement failure (tick 1)")
            return await real_apply(*a, **kw)

        monkeypatch.setattr(subs, "apply_settlement", flaky_apply)

        # Tick 1: apply_settlement fails -> wake must NOT be delivered, the
        # row must stay eligible for retry, and paid_through must be
        # untouched (the extension never applied).
        out1 = await watcher.tick_once()
        assert out1["settled_notified"] == 0
        assert agent.wakes == []
        assert agent.correspondent_deliveries == []

        row1 = await subs.get_subscription(sub["id"], db=db)
        assert row1["paid_through"] == now  # NOT extended yet

        still_unnotified = await invoicing.settled_unnotified_invoices(db=db)
        assert [r["request_id"] for r in still_unnotified] == [inv["request_id"]]

        # Tick 2: apply_settlement succeeds this time -> the SAME invoice is
        # retried, the extension applies, and the wake fires exactly once.
        out2 = await watcher.tick_once()
        assert out2["settled_notified"] == 1
        assert len(agent.wakes) == 1

        row2 = await subs.get_subscription(sub["id"], db=db)
        assert row2["paid_through"] == now + 30 * 86400

        # Tick 3: nothing left to do — no double-notify, no double-extend.
        out3 = await watcher.tick_once()
        assert out3["settled_notified"] == 0
        assert len(agent.wakes) == 1
        row3 = await subs.get_subscription(sub["id"], db=db)
        assert row3["paid_through"] == now + 30 * 86400
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_settlement_failure_does_not_livelock_other_invoices(tmp_path, monkeypatch):
    """A permanently-failing apply_settlement for ONE subscription's invoice
    must not block notification of a DIFFERENT, healthy settled invoice in
    the same tick — only the failing invoice is skipped."""
    db = await _setup_db(tmp_path)
    try:
        sub_bad = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="bad@x.com", cron_job_id="job_bad",
            period_days=30, paid_through=int(time.time()), db=db)
        inv_bad = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_bad", amount_usd=10.0,
            purpose="renewal", subscription_id=sub_bad["id"], db=db)
        await invoicing.settle_payment_request(inv_bad["request_id"], db=db)

        # A plain, non-subscription invoice settling in the SAME tick.
        inv_ok = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_ok", amount_usd=3.0,
            purpose="one-off", db=db)
        await invoicing.settle_payment_request(inv_ok["request_id"], db=db)

        real_apply = subs.apply_settlement

        async def always_fails(subscription_id, request_id, *, db=None):
            if subscription_id == sub_bad["id"]:
                raise RuntimeError("permanently broken for this subscription")
            return await real_apply(subscription_id, request_id, db=db)

        monkeypatch.setattr(subs, "apply_settlement", always_fails)

        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()

        # The healthy invoice still gets notified this tick.
        assert out["settled_notified"] == 1
        assert len(agent.wakes) == 1
        assert agent.wakes[0][3]["request_id"] == inv_ok["request_id"]

        # The bad one stays unnotified/un-extended, forever retried, never crashing.
        still = await invoicing.settled_unnotified_invoices(db=db)
        assert [r["request_id"] for r in still] == [inv_bad["request_id"]]

        out2 = await watcher.tick_once()  # must not raise even on repeat failure
        assert out2["settled_notified"] == 0
    finally:
        await db.close()


# --- Task 14 fix pass 2, Finding 1: atomic apply_settlement + typed result --

@pytest.mark.asyncio
async def test_apply_settlement_second_statement_failure_rolls_back_and_wake_fires_once(
        tmp_path, monkeypatch):
    """The full lost-renewal-extension regression, end-to-end through the
    settlement watcher's REAL tick (not a mocked apply_settlement): a crash
    between the ledger INSERT and the paid_through UPDATE inside
    `subscriptions.apply_settlement` must roll back BOTH statements
    together, not just leave the ledger claim behind.

    Against the pre-fix (non-atomic) code, tick 1 would commit the ledger
    row independently of the UPDATE; tick 2's retry would then hit the
    ledger PK and return `False` WITHOUT raising -- `settlement_watcher`'s
    `continue` only fired on an EXCEPTION, so it would fall through to
    claim_wake/_notify, marking the invoice notified while `paid_through`
    stayed PERMANENTLY stuck at its pre-renewal value. Money lost, silently.
    This test FAILS against that code (tick 2 asserts extension applied
    would fail, since the pre-fix code returns False on tick 2's retry and
    the row is never extended).

    With the fix: tick 1's failed UPDATE rolls back the ledger INSERT too,
    so tick 2 retries cleanly and the wake fires exactly once, with the
    extension applied exactly once."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="p@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_atomic", amount_usd=10.0,
            purpose="renewal", subscription_id=sub["id"], db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)

        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))

        real_execute = db.execute
        state = {"fail_next_update": True}

        async def flaky_execute(query, params=()):
            if (state["fail_next_update"]
                    and query.strip().upper().startswith("UPDATE SUBSCRIPTIONS SET PAID_THROUGH")):
                state["fail_next_update"] = False
                raise RuntimeError("simulated crash between ledger INSERT and paid_through UPDATE")
            return await real_execute(query, params)

        monkeypatch.setattr(db, "execute", flaky_execute)

        # Tick 1: apply_settlement's UPDATE fails -- the ledger claim must
        # roll back too. No wake, no stale ledger row.
        out1 = await watcher.tick_once()
        assert out1["settled_notified"] == 0
        assert agent.wakes == []

        ledger_row = await db.fetch_one(
            "SELECT * FROM subscription_applied_settlements WHERE request_id = ?",
            (inv["request_id"],))
        assert ledger_row is None  # rolled back -- NOT a stale ledger claim

        row1 = await subs.get_subscription(sub["id"], db=db)
        assert row1["paid_through"] == now  # untouched -- extension never applied

        # Tick 2: the one-shot failure has been consumed -- the retry
        # applies cleanly and the wake fires exactly once.
        out2 = await watcher.tick_once()
        assert out2["settled_notified"] == 1
        assert len(agent.wakes) == 1

        row2 = await subs.get_subscription(sub["id"], db=db)
        assert row2["paid_through"] == now + 30 * 86400  # extended EXACTLY once

        # Tick 3: nothing left -- no double-notify, no double-extend.
        out3 = await watcher.tick_once()
        assert out3["settled_notified"] == 0
        assert len(agent.wakes) == 1
        row3 = await subs.get_subscription(sub["id"], db=db)
        assert row3["paid_through"] == now + 30 * 86400
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_already_applied_result_proceeds_to_wake_without_double_extending(tmp_path):
    """ALREADY_APPLIED (a PK conflict from a genuinely-prior-complete apply)
    must still let the watcher proceed to claim the wake -- the extension is
    guaranteed to have landed already, atomically, so there is nothing
    unsafe about notifying."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="p@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_dup", amount_usd=10.0,
            purpose="renewal", subscription_id=sub["id"], db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)

        # A prior call already fully applied this exact request_id (e.g. a
        # process that settled + applied but crashed before marking the
        # wake claimed).
        pre = await subs.apply_settlement(sub["id"], inv["request_id"], db=db)
        assert pre == subs.SettlementResult.APPLIED
        row_pre = await subs.get_subscription(sub["id"], db=db)
        assert row_pre["paid_through"] == now + 30 * 86400

        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()
        assert out["settled_notified"] == 1  # ALREADY_APPLIED -> still wakes
        assert len(agent.wakes) == 1

        row = await subs.get_subscription(sub["id"], db=db)
        assert row["paid_through"] == now + 30 * 86400  # not double-extended
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_refused_result_does_not_silently_wake_as_settled(tmp_path):
    """REFUSED (F3 tenant mismatch) must never produce an ordinary
    `payment_settled`-shaped "continue your work" wake -- the money arrived
    but the subscription could not be extended, which is an owner-actionable
    anomaly. The watcher must claim the wake (so this terminal, unfixable-
    by-retry outcome doesn't spin forever) but must NOT deliver the normal
    settlement wake, and must not leave `paid_through` touched."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub_b = await subs.create_subscription(
            user_id="tenant_b", correspondent_surface="email",
            correspondent_address="b@x.com", cron_job_id="job_b",
            period_days=30, paid_through=now, db=db)
        # tenant_a's invoice references tenant_b's subscription -- F3 refuses.
        inv_a = await invoicing.create_payment_request(
            user_id="tenant_a", session_id="sess_refused", amount_usd=10.0,
            purpose="renewal", subscription_id=sub_b["id"], db=db)
        await invoicing.settle_payment_request(inv_a["request_id"], db=db)

        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()

        # The wake was claimed for this invoice (terminal outcome), but it
        # must NOT look like an ordinary settlement wake -- the anomaly
        # notice goes over the owner delivery rail, never `deliver_self_wake`
        # / `deliver_correspondent_data`.
        assert out["settled_notified"] == 1
        assert agent.wakes == []  # NOT the ordinary "settled, continue" wake
        assert agent.correspondent_deliveries == []

        row = await subs.get_subscription(sub_b["id"], db=db)
        assert row["paid_through"] == now  # untouched -- refused, not extended

        # The invoice's wake is claimed (won't retry forever chasing an
        # unfixable mismatch) -- a second tick has nothing left to do.
        out2 = await watcher.tick_once()
        assert out2["settled_notified"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unknown_subscription_does_not_silently_wake_as_settled(tmp_path):
    """UNKNOWN (subscription_id no longer resolves to a real row) must also
    never silently wake as an ordinary settlement -- same anomaly handling
    as REFUSED."""
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_unknown", amount_usd=10.0,
            purpose="renewal", subscription_id="sub_does_not_exist", db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)

        agent = _Agent()
        watcher = SettlementWatcher(agent, db=db, goal_board=_board(tmp_path))
        out = await watcher.tick_once()

        assert out["settled_notified"] == 1  # wake claimed (terminal anomaly)
        assert agent.wakes == []  # NOT the ordinary "settled, continue" wake

        out2 = await watcher.tick_once()
        assert out2["settled_notified"] == 0  # nothing left to retry
    finally:
        await db.close()
