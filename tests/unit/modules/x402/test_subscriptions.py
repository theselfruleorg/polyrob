"""Watchtower subscriptions store (Task 14, Phase 3 R5).

Runs against the REAL x402/subscriptions schema. Contract: tenant-scoped CRUD,
settlement application is idempotent (keyed on request_id), and the renewal/
lapse sweep queries return exactly the rows the mechanics need.
"""
import asyncio
import time

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing, subscriptions as subs


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")


# --- flags -------------------------------------------------------------------

def test_flags_default_off_and_documented_defaults(monkeypatch):
    for var in ("SUBSCRIPTIONS_ENABLED", "WATCHTOWER_PRICE_USD",
                "SUBSCRIPTION_RENEWAL_LEAD_DAYS", "SUBSCRIPTION_GRACE_DAYS"):
        monkeypatch.delenv(var, raising=False)
    assert subs.subscriptions_enabled() is False
    assert subs.watchtower_price_usd() == 10.00
    assert subs.subscription_renewal_lead_days() == 5
    assert subs.subscription_grace_days() == 3


def test_flags_read_env_overrides(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
    monkeypatch.setenv("WATCHTOWER_PRICE_USD", "25")
    monkeypatch.setenv("SUBSCRIPTION_RENEWAL_LEAD_DAYS", "7")
    monkeypatch.setenv("SUBSCRIPTION_GRACE_DAYS", "1")
    assert subs.subscriptions_enabled() is True
    assert subs.watchtower_price_usd() == 25.0
    assert subs.subscription_renewal_lead_days() == 7
    assert subs.subscription_grace_days() == 1


# --- CRUD + tenant scoping ---------------------------------------------------

@pytest.mark.asyncio
async def test_create_persists_active_row_with_defaults(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="payer@example.com", cron_job_id="job1", db=db)
        assert sub["id"].startswith("sub_")
        assert sub["status"] == subs.STATUS_ACTIVE
        assert sub["amount_usd"] == 10.00  # WATCHTOWER_PRICE_USD default
        assert sub["period_days"] == 30
        assert sub["renewal_lead_days"] == 5
        assert sub["grace_days"] == 3
        assert sub["paid_through"] > time.time()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_refuses_anonymous_tenant(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError):
            await subs.create_subscription(
                user_id="", correspondent_surface="email",
                correspondent_address="p@x.com", cron_job_id="job1", db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_and_list_are_tenant_scoped(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        a = await subs.create_subscription(
            user_id="tenant_a", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job_a", db=db)
        b = await subs.create_subscription(
            user_id="tenant_b", correspondent_surface="email",
            correspondent_address="b@x.com", cron_job_id="job_b", db=db)

        # Tenant A cannot read tenant B's row via the tenant-scoped getter.
        assert await subs.get_subscription(b["id"], user_id="tenant_a", db=db) is None
        assert await subs.get_subscription(a["id"], user_id="tenant_a", db=db) is not None
        # Un-scoped get (internal/system use) still resolves either row.
        assert (await subs.get_subscription(b["id"], db=db))["id"] == b["id"]

        list_a = await subs.list_subscriptions(user_id="tenant_a", db=db)
        list_b = await subs.list_subscriptions(user_id="tenant_b", db=db)
        assert [r["id"] for r in list_a] == [a["id"]]
        assert [r["id"] for r in list_b] == [b["id"]]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_is_tenant_scoped(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        a = await subs.create_subscription(
            user_id="tenant_a", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job_a", db=db)

        # Tenant B cannot cancel tenant A's subscription.
        assert await subs.cancel_subscription(a["id"], user_id="tenant_b", db=db) is False
        row = await subs.get_subscription(a["id"], db=db)
        assert row["status"] == subs.STATUS_ACTIVE

        # The owning tenant can.
        assert await subs.cancel_subscription(a["id"], user_id="tenant_a", db=db) is True
        row = await subs.get_subscription(a["id"], db=db)
        assert row["status"] == subs.STATUS_CANCELED

        # Idempotent: canceling an already-canceled row is a no-op False.
        assert await subs.cancel_subscription(a["id"], user_id="tenant_a", db=db) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_empty_or_anonymous_returns_nothing(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await subs.create_subscription(
            user_id="tenant_a", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job_a", db=db)
        assert await subs.list_subscriptions(user_id="", db=db) == []
    finally:
        await db.close()


# --- cron-gate predicate -----------------------------------------------------

@pytest.mark.asyncio
async def test_subscription_permits_work_by_status(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1", db=db)
        assert await subs.subscription_permits_work(sub["id"], db=db) is True

        await db.execute("UPDATE subscriptions SET status='grace' WHERE id=?", (sub["id"],))
        assert await subs.subscription_permits_work(sub["id"], db=db) is True

        await db.execute("UPDATE subscriptions SET status='suspended' WHERE id=?", (sub["id"],))
        assert await subs.subscription_permits_work(sub["id"], db=db) is False

        await db.execute("UPDATE subscriptions SET status='canceled' WHERE id=?", (sub["id"],))
        assert await subs.subscription_permits_work(sub["id"], db=db) is False

    finally:
        await db.close()


@pytest.mark.asyncio
async def test_subscription_permits_work_missing_id_is_permissive(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        assert await subs.subscription_permits_work("sub_doesnotexist", db=db) is True
    finally:
        await db.close()


# --- apply_settlement idempotency --------------------------------------------

@pytest.mark.asyncio
async def test_apply_settlement_extends_paid_through_once(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)
        applied = await subs.apply_settlement(sub["id"], "req_1", db=db)
        assert applied == subs.SettlementResult.APPLIED
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["paid_through"] == now + 30 * 86400
        assert row["status"] == subs.STATUS_ACTIVE

        # Re-processing the SAME request_id must NOT double-extend.
        applied_again = await subs.apply_settlement(sub["id"], "req_1", db=db)
        assert applied_again == subs.SettlementResult.ALREADY_APPLIED
        row2 = await subs.get_subscription(sub["id"], db=db)
        assert row2["paid_through"] == now + 30 * 86400

        # A DIFFERENT request_id (the next renewal cycle) extends again.
        applied2 = await subs.apply_settlement(sub["id"], "req_2", db=db)
        assert applied2 == subs.SettlementResult.APPLIED
        row3 = await subs.get_subscription(sub["id"], db=db)
        assert row3["paid_through"] == now + 60 * 86400
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_settlement_reactivates_suspended_sub(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1",
            period_days=30, paid_through=now - 100000, db=db)
        await db.execute("UPDATE subscriptions SET status='suspended' WHERE id=?", (sub["id"],))

        applied = await subs.apply_settlement(sub["id"], "req_x", db=db)
        assert applied == subs.SettlementResult.APPLIED
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["status"] == subs.STATUS_ACTIVE
        assert row["paid_through"] == (now - 100000) + 30 * 86400
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_settlement_unknown_subscription_is_noop(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        assert await subs.apply_settlement("sub_nope", "req_1", db=db) == subs.SettlementResult.UNKNOWN
    finally:
        await db.close()


# --- Task 14 review Finding 3: tenant-mismatch defense-in-depth --------------

@pytest.mark.asyncio
async def test_apply_settlement_refuses_cross_tenant_invoice(tmp_path):
    """A settled invoice belonging to tenant A must never extend tenant B's
    subscription, even if it somehow carries tenant B's subscription_id
    (unexploitable today -- only the settlement watcher ever writes
    metadata.subscription_id, always matching the invoice's own tenant -- but
    apply_settlement must not trust that blindly). Refusal must not consume
    the idempotency ledger key either, so a legitimate future call for the
    CORRECT subscription/tenant pairing is never blocked by this refusal."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub_b = await subs.create_subscription(
            user_id="tenant_b", correspondent_surface="email",
            correspondent_address="b@x.com", cron_job_id="job_b",
            period_days=30, paid_through=now, db=db)
        inv_a = await invoicing.create_payment_request(
            user_id="tenant_a", session_id="", amount_usd=10.0,
            purpose="renewal", subscription_id=sub_b["id"], db=db)
        await invoicing.settle_payment_request(inv_a["request_id"], db=db)

        applied = await subs.apply_settlement(sub_b["id"], inv_a["request_id"], db=db)
        assert applied == subs.SettlementResult.REFUSED

        row = await subs.get_subscription(sub_b["id"], db=db)
        assert row["paid_through"] == now  # untouched — no extension applied
        assert row["status"] == subs.STATUS_ACTIVE  # untouched too

        # The refusal must not have consumed the idempotency ledger — a later
        # legitimate call for the correct tenant/subscription must still work.
        ledger_row = await db.fetch_one(
            "SELECT * FROM subscription_applied_settlements WHERE request_id = ?",
            (inv_a["request_id"],))
        assert ledger_row is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_settlement_matching_tenant_still_extends(tmp_path):
    """Sanity: the Finding 3 tenant check must not break the ordinary,
    matching-tenant path — a real invoice whose tenant matches its
    subscription's owner still extends normally."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=10.0, purpose="renewal",
            subscription_id=sub["id"], db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)

        applied = await subs.apply_settlement(sub["id"], inv["request_id"], db=db)
        assert applied == subs.SettlementResult.APPLIED
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["paid_through"] == now + 30 * 86400
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_settlement_permissive_when_invoice_row_unresolvable(tmp_path):
    """A synthetic/unresolvable request_id (no matching x402_payment_requests
    row -- e.g. a test double, or a pre-invoicing-era ledger row) must stay
    permissive: nothing to compare tenants against, so it is NOT treated as a
    mismatch. This preserves the existing apply_settlement idempotency tests
    that call it directly with made-up request_ids."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)
        applied = await subs.apply_settlement(sub["id"], "req_never_invoiced", db=db)
        assert applied == subs.SettlementResult.APPLIED
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["paid_through"] == now + 30 * 86400
    finally:
        await db.close()


# --- Task 14 fix pass 2, Finding 1: atomicity of the ledger claim + extend ---

@pytest.mark.asyncio
async def test_apply_settlement_second_statement_failure_rolls_back_ledger(tmp_path, monkeypatch):
    """The lost-renewal-extension bug: `apply_settlement` used to do the
    ledger INSERT and the `paid_through` UPDATE as two INDEPENDENTLY
    auto-committed statements (`DatabaseConnection.execute` auto-commits
    each statement unless inside an explicit transaction). If the SECOND
    statement failed (crash, `asyncio.CancelledError` at shutdown, "database
    is locked" — this connection has no WAL/busy_timeout), the ledger
    INSERT had ALREADY committed independently, leaving a ledger row behind
    with NO extension ever applied. A retry with the SAME request_id then
    hit that ledger row's PRIMARY KEY and returned False WITHOUT raising —
    silently indistinguishable from "refused" — so the caller had no way to
    tell "extension never applied" apart from "already fully applied".

    This test fails against the pre-fix code (a ledger row survives tick 1,
    and the tick-2 retry incorrectly reports "already applied" for an
    extension that never happened). With the atomic fix, the failed UPDATE
    rolls back the ledger INSERT too, so NO row survives tick 1 and tick 2's
    retry applies the extension cleanly, exactly once."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)

        real_execute = db.execute
        state = {"fail_next_update": True}

        async def flaky_execute(query, params=()):
            if (state["fail_next_update"]
                    and query.strip().upper().startswith("UPDATE SUBSCRIPTIONS SET PAID_THROUGH")):
                state["fail_next_update"] = False
                raise RuntimeError("simulated crash between ledger INSERT and paid_through UPDATE")
            return await real_execute(query, params)

        monkeypatch.setattr(db, "execute", flaky_execute)

        # Tick 1: the ledger INSERT succeeds, then the paid_through UPDATE
        # raises. The whole transaction must roll back — no partial state.
        with pytest.raises(RuntimeError):
            await subs.apply_settlement(sub["id"], "req_1", db=db)

        ledger_row = await db.fetch_one(
            "SELECT * FROM subscription_applied_settlements WHERE request_id = ?",
            ("req_1",))
        assert ledger_row is None  # rolled back — NOT a stale ledger claim

        row1 = await subs.get_subscription(sub["id"], db=db)
        assert row1["paid_through"] == now  # untouched — extension never applied

        # Tick 2: the SAME request_id retried — the one-shot failure has
        # been consumed, so this call must apply cleanly.
        result2 = await subs.apply_settlement(sub["id"], "req_1", db=db)
        assert result2 == subs.SettlementResult.APPLIED
        row2 = await subs.get_subscription(sub["id"], db=db)
        assert row2["paid_through"] == now + 30 * 86400  # extended exactly once

        # Tick 3: a further replay of the SAME request_id must be idempotent
        # (already applied), never double-extending.
        result3 = await subs.apply_settlement(sub["id"], "req_1", db=db)
        assert result3 == subs.SettlementResult.ALREADY_APPLIED
        row3 = await subs.get_subscription(sub["id"], db=db)
        assert row3["paid_through"] == now + 30 * 86400
    finally:
        await db.close()


# --- Task 14 fix pass 3, re-review Finding: CancelledError transaction leak --

@pytest.mark.asyncio
async def test_apply_settlement_cancelled_error_rolls_back_and_propagates(tmp_path, monkeypatch):
    """asyncio.CancelledError derives from BaseException (NOT Exception) since
    Python 3.8 — `issubclass(asyncio.CancelledError, Exception)` is False. If
    the settlement watcher ticker is force-cancelled (autonomy-runtime
    shutdown, ``core/autonomy_runtime.py``'s ``_STOP_GRACE_SEC`` timeout)
    while `apply_settlement` is awaiting the `paid_through` UPDATE INSIDE its
    transaction, a bare `except Exception:` lets the cancellation skip
    straight past the rollback. That leaves
    `DatabaseConnection._in_transaction` PERMANENTLY True on the connection
    instance passed in — poisoning it for the rest of process life, since
    `execute()` only auto-commits when NOT `_in_transaction`. In prod this
    connection is a shared singleton used by every x402/credits/
    user_profiles write, so the blast radius is a silent durability outage
    for the whole bot.db, not just this one subscription.

    This test fails against the pre-fix code (a bare `except Exception:`):
    the CancelledError propagates WITHOUT rolling back, leaving
    `_in_transaction` stuck True and a stale ledger row behind. With the fix
    (`except (asyncio.CancelledError, Exception):`), the cancellation still
    propagates (never swallowed) but the transaction is rolled back first,
    so the connection is left usable and no partial state survives."""
    db = await _setup_db(tmp_path)
    now = int(time.time())
    sub = await subs.create_subscription(
        user_id="rob", correspondent_surface="email",
        correspondent_address="a@x.com", cron_job_id="job1",
        period_days=30, paid_through=now, db=db)

    real_execute = db.execute
    state = {"cancel_next_update": True}

    async def flaky_execute(query, params=()):
        if (state["cancel_next_update"]
                and query.strip().upper().startswith("UPDATE SUBSCRIPTIONS SET PAID_THROUGH")):
            state["cancel_next_update"] = False
            raise asyncio.CancelledError()
        return await real_execute(query, params)

    monkeypatch.setattr(db, "execute", flaky_execute)

    # (a) the CancelledError must propagate — cancellation is never
    # swallowed by this fix, only cleaned up after.
    with pytest.raises(asyncio.CancelledError):
        await subs.apply_settlement(sub["id"], "req_cancel", db=db)

    # (b) the connection must NOT be left poisoned: _in_transaction must be
    # False afterward, so a subsequent, unrelated write on the SAME
    # connection instance goes back to auto-committing.
    assert await db.in_transaction() is False

    # Prove auto-commit durability with a FRESH connection to the same file
    # — reading back through the SAME (possibly-poisoned) connection would
    # show its own uncommitted write regardless (sqlite's own-connection
    # visibility), so only a brand-new connection actually distinguishes
    # "committed to disk" from "sitting in an open, never-committed
    # transaction".
    await db.execute(
        "UPDATE subscriptions SET status = ? WHERE id = ?",
        (subs.STATUS_SUSPENDED, sub["id"]),
    )
    await db.close()

    db2 = DatabaseConnection(tmp_path / "bot.db")
    await db2.connect()
    try:
        row = await db2.fetch_one(
            "SELECT * FROM subscriptions WHERE id = ?", (sub["id"],))
        assert row["status"] == subs.STATUS_SUSPENDED
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_apply_settlement_cancelled_error_no_stale_ledger_row(tmp_path, monkeypatch):
    """(c) Companion assertion to the test above, kept separate for a clean
    failure signal: a CancelledError mid-UPDATE must roll back the ledger
    INSERT too — no stale `subscription_applied_settlements` row survives,
    exactly like the RuntimeError case fix pass 2 already covers."""
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1",
            period_days=30, paid_through=now, db=db)

        real_execute = db.execute
        state = {"cancel_next_update": True}

        async def flaky_execute(query, params=()):
            if (state["cancel_next_update"]
                    and query.strip().upper().startswith("UPDATE SUBSCRIPTIONS SET PAID_THROUGH")):
                state["cancel_next_update"] = False
                raise asyncio.CancelledError()
            return await real_execute(query, params)

        monkeypatch.setattr(db, "execute", flaky_execute)

        with pytest.raises(asyncio.CancelledError):
            await subs.apply_settlement(sub["id"], "req_cancel2", db=db)

        ledger_row = await db.fetch_one(
            "SELECT * FROM subscription_applied_settlements WHERE request_id = ?",
            ("req_cancel2",))
        assert ledger_row is None  # rolled back — NOT a stale ledger claim

        row = await subs.get_subscription(sub["id"], db=db)
        assert row["paid_through"] == now  # untouched — extension never applied

        # A retry with the same request_id (as the watcher would do on its
        # next tick) must apply cleanly — proving the connection is not
        # wedged and the transaction state is clean.
        result2 = await subs.apply_settlement(sub["id"], "req_cancel2", db=db)
        assert result2 == subs.SettlementResult.APPLIED
        row2 = await subs.get_subscription(sub["id"], db=db)
        assert row2["paid_through"] == now + 30 * 86400
    finally:
        await db.close()


# --- renewal/lapse sweep queries ---------------------------------------------

@pytest.mark.asyncio
async def test_needing_renewal_respects_lead_window_and_open_invoice(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        # Due: inside the 5-day lead window (paid_through in 2 days).
        due = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job_due",
            renewal_lead_days=5, paid_through=now + 2 * 86400, db=db)
        # Not due: paid_through far in the future.
        await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="b@x.com", cron_job_id="job_not_due",
            renewal_lead_days=5, paid_through=now + 20 * 86400, db=db)

        rows = await subs.subscriptions_needing_renewal(now=now, db=db)
        assert [r["id"] for r in rows] == [due["id"]]

        # Now attach an OPEN pending renewal invoice for `due` — it must drop
        # out of the needing-renewal set.
        await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=due["amount_usd"],
            purpose="renewal", subscription_id=due["id"], db=db)
        rows2 = await subs.subscriptions_needing_renewal(now=now, db=db)
        assert rows2 == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_needing_renewal_excludes_suspended_and_canceled(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1",
            renewal_lead_days=5, paid_through=now + 1, db=db)
        await db.execute("UPDATE subscriptions SET status='suspended' WHERE id=?", (sub["id"],))
        assert await subs.subscriptions_needing_renewal(now=now, db=db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_to_grace_transitions_once_and_emits(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        sub = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1",
            paid_through=now - 10, db=db)
        graced = await subs.subscriptions_to_grace(now=now, db=db)
        assert [r["id"] for r in graced] == [sub["id"]]
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["status"] == subs.STATUS_GRACE

        # A second sweep does not re-flip / re-report an already-grace row.
        graced2 = await subs.subscriptions_to_grace(now=now, db=db)
        assert graced2 == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_to_suspend_transitions_active_or_grace_past_grace_window(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        now = int(time.time())
        grace_days = 3
        # Still active, but so far past paid_through that grace is ALSO elapsed.
        overdue = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="a@x.com", cron_job_id="job1",
            grace_days=grace_days, paid_through=now - (grace_days + 1) * 86400, db=db)
        # Genuinely still within grace — must NOT be suspended yet.
        within_grace = await subs.create_subscription(
            user_id="rob", correspondent_surface="email",
            correspondent_address="b@x.com", cron_job_id="job2",
            grace_days=grace_days, paid_through=now - 1, db=db)
        await db.execute("UPDATE subscriptions SET status='grace' WHERE id=?", (within_grace["id"],))

        suspended = await subs.subscriptions_to_suspend(now=now, db=db)
        assert [r["id"] for r in suspended] == [overdue["id"]]
        row = await subs.get_subscription(within_grace["id"], db=db)
        assert row["status"] == subs.STATUS_GRACE  # untouched

        # subscriptions_to_suspend does NOT emit its own event (the watcher owns
        # emission alongside notices) — nothing to assert on the event log here,
        # just that the pure transition happened.
        row2 = await subs.get_subscription(overdue["id"], db=db)
        assert row2["status"] == subs.STATUS_SUSPENDED
    finally:
        await db.close()
