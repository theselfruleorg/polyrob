"""Money loop — agent-initiated payment requests (invoices).

Runs against the REAL x402 schema (the all-fakes pattern is why N1 shipped).
Contract: pending rows carry kind=agent_invoice + session provenance in
metadata; amounts are ceiling-bounded; creation is per-day capped; settlement is
an explicit pending→completed transition; expiry honors the deadline column.
"""
import json

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_create_payment_request_refused_while_halted(tmp_path, monkeypatch):
    """H5: the owner kill-switch blocks minting new payment requests too (incl.
    auto-mode renewals from the settlement watcher), not just outbound spend."""
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    db = await _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="HALTED"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="sess_1", amount_usd=5.0,
                purpose="research report", db=db,
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_persists_pending_invoice_row(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_1", amount_usd=5.0,
            purpose="research report", db=db,
        )
        assert inv["status"] == "pending"
        assert inv["recipient"] == "0xTREASURY"
        row = await db.fetch_one(
            "SELECT * FROM x402_payment_requests WHERE id = ?", (inv["request_id"],))
        assert row is not None and row["status"] == "pending"
        # DatabaseConnection.fetch_* auto-parses JSON-looking TEXT columns
        meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        assert meta["kind"] == "agent_invoice"
        assert meta["session_id"] == "sess_1"
        assert meta["tenant_id"] == "rob"
        assert meta["wake_delivered"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_payment_request_returns_row(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        created = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="test", db=db)
        got = await invoicing.get_payment_request(created["request_id"], db=db)
        assert got is not None
        assert got["request_id"] == created["request_id"]
        assert got["amount_usd"] == 5.0
        assert got["status"] == "pending"
        assert got["recipient"] and got["chain"] and got["nonce"]
        assert got["purpose"] == "test"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_payment_request_missing_returns_none(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        assert await invoicing.get_payment_request("inv_doesnotexist", db=db) is None
        assert await invoicing.get_payment_request("", db=db) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_stores_correspondent_ref(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=3.0, purpose="svc", db=db,
            correspondent_ref={"surface": "email", "address": "x@y.z", "thread_id": ""})
        rows = await invoicing.settled_unnotified_invoices(db=db)  # none settled yet
        assert rows == []
        # settle then read the linkage through the watcher's read path
        await invoicing.settle_payment_request(inv["request_id"], transaction_hash="0xabc", db=db)
        settled = await invoicing.settled_unnotified_invoices(db=db)
        assert len(settled) == 1
        assert settled[0]["correspondent_ref"] == {
            "surface": "email", "address": "x@y.z", "thread_id": ""}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_drops_invalid_correspondent_ref(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=3.0, purpose="svc", db=db,
            correspondent_ref={"surface": "", "address": "x@y.z"})
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        settled = await invoicing.settled_unnotified_invoices(db=db)
        assert settled[0]["correspondent_ref"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_amount_ceiling_enforced(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        monkeypatch.setenv("X402_INVOICE_MAX_USD", "10")
        with pytest.raises(ValueError, match="ceiling"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s", amount_usd=11.0, purpose="too big", db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_daily_cap_enforced(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        monkeypatch.setenv("X402_INVOICE_DAILY_MAX", "2")
        for i in range(2):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s", amount_usd=1.0, purpose=f"job {i}", db=db)
        with pytest.raises(ValueError, match="daily"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s", amount_usd=1.0, purpose="job 3", db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_treasury_refused(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "")
        with pytest.raises(ValueError, match="treasury"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s", amount_usd=1.0, purpose="p", db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settle_transitions_pending_once(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=2.0, purpose="p", db=db)
        rid = inv["request_id"]
        assert await invoicing.settle_payment_request(rid, transaction_hash="0xabc", db=db) is True
        # idempotent: second settle is a no-op
        assert await invoicing.settle_payment_request(rid, db=db) is False
        row = await db.fetch_one("SELECT * FROM x402_payment_requests WHERE id=?", (rid,))
        assert row["status"] == "completed" and row["transaction_hash"] == "0xabc"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expiry_marks_stale_pending(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=2.0, purpose="p",
            expiry_hours=1.0, db=db)
        # nothing expires yet
        assert await invoicing.expire_stale_requests(db=db) == []
        # jump past the deadline
        expired = await invoicing.expire_stale_requests(
            db=db, now=inv["expires_at_epoch"] + 10)
        assert [e["request_id"] for e in expired] == [inv["request_id"]]
        row = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id=?", (inv["request_id"],))
        assert row["status"] == "expired"
        # an expired invoice can no longer be settled
        assert await invoicing.settle_payment_request(inv["request_id"], db=db) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settled_unnotified_and_mark_delivered(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_9", amount_usd=3.0, purpose="p", db=db)
        assert await invoicing.settled_unnotified_invoices(db=db) == []
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        pending_wakes = await invoicing.settled_unnotified_invoices(db=db)
        assert len(pending_wakes) == 1
        assert pending_wakes[0]["session_id"] == "sess_9"
        assert pending_wakes[0]["user_id"] == "rob"
        await invoicing.mark_wake_delivered(inv["request_id"], db=db)
        assert await invoicing.settled_unnotified_invoices(db=db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_is_tenant_scoped(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=1.0, purpose="mine", db=db)
        await invoicing.create_payment_request(
            user_id="other", session_id="s2", amount_usd=1.0, purpose="theirs", db=db)
        mine = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert len(mine) == 1 and mine[0]["purpose"] == "mine"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_tenant_match_does_not_wildcard_leak(tmp_path):
    """G-14: the tenant leg used `metadata LIKE '%"tenant_id": "<id>"%'` — SQLite
    LIKE treats `_`/`%` as wildcards, and real tenant ids contain underscores
    (u_<hex>, core/identity.py). The ROW belongs to the non-underscore
    lookalike tenant 'uXabc'; the QUERY runs as 'u_abc' — it is the '_' in the
    QUERYING id that the old LIKE pattern read as a live wildcard (matching
    'X'), so 'u_abc' would incorrectly see 'uXabc's row. Must be an exact
    json_extract match."""
    db = await _setup_db(tmp_path)
    try:
        await invoicing.create_payment_request(
            user_id="uXabc", session_id="s1", amount_usd=1.0, purpose="theirs", db=db)
        # Querying as 'u_abc' is exactly what SQLite LIKE '%"tenant_id": "u_abc"%'
        # would also match against 'uXabc's row if '_' were left as a live
        # wildcard (u -> X substitution).
        leaked = await invoicing.list_payment_requests(user_id="u_abc", db=db)
        assert leaked == []
        theirs = await invoicing.list_payment_requests(user_id="uXabc", db=db)
        assert len(theirs) == 1 and theirs[0]["purpose"] == "theirs"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_daily_cap_tenant_match_does_not_wildcard_leak(tmp_path, monkeypatch):
    """Same bug class in the daily-cap COUNT query: a lookalike tenant id's
    invoice must never be counted against another tenant's daily cap. The
    lookalike 'uXabc' creates its own row first; 'u_abc' (the underscore-
    bearing id) must still get a fresh cap for its very FIRST invoice — pre-fix,
    the '_' in u_abc's LIKE pattern was a live wildcard that counted uXabc's row
    against u_abc, incorrectly refusing it."""
    db = await _setup_db(tmp_path)
    try:
        monkeypatch.setenv("X402_INVOICE_DAILY_MAX", "1")
        await invoicing.create_payment_request(
            user_id="uXabc", session_id="s2", amount_usd=1.0, purpose="theirs", db=db)
        await invoicing.create_payment_request(
            user_id="u_abc", session_id="s1", amount_usd=1.0, purpose="mine", db=db)
        mine = await invoicing.list_payment_requests(user_id="u_abc", db=db)
        assert len(mine) == 1 and mine[0]["purpose"] == "mine"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_empty_tenant_refused_everywhere(tmp_path):
    # An empty user_id would create a SHARED anonymous bucket (cross-tenant
    # reads + shared daily cap) — creation raises, listing returns nothing.
    db = await _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="authenticated tenant"):
            await invoicing.create_payment_request(
                user_id="", session_id="s", amount_usd=1.0, purpose="p", db=db)
        assert await invoicing.list_payment_requests(user_id="", db=db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_claim_wake_is_atomic_single_winner(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=1.0, purpose="p", db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        assert await invoicing.claim_wake(inv["request_id"], db=db) is True
        # a racing second claimer loses
        assert await invoicing.claim_wake(inv["request_id"], db=db) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_claim_for_settlement_is_exclusive(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        rid = inv["request_id"]
        # first claim wins, second loses (already settling)
        assert await invoicing.claim_for_settlement(rid, db=db) is True
        assert await invoicing.claim_for_settlement(rid, db=db) is False
        # settle works from 'settling'
        assert await invoicing.settle_payment_request(rid, transaction_hash="0xt", db=db) is True
        # now settled -> claim fails
        assert await invoicing.claim_for_settlement(rid, db=db) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_revert_settlement_claim_restores_pending(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        rid = inv["request_id"]
        assert await invoicing.claim_for_settlement(rid, db=db) is True
        await invoicing.revert_settlement_claim(rid, db=db)
        got = await invoicing.get_payment_request(rid, db=db)
        assert got["status"] == "pending"  # payable again
        # a completed row is never resurrected by revert
        await invoicing.claim_for_settlement(rid, db=db)
        await invoicing.settle_payment_request(rid, db=db)
        await invoicing.revert_settlement_claim(rid, db=db)
        got = await invoicing.get_payment_request(rid, db=db)
        assert got["status"] == "completed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_owner_direct_settle_still_works_from_pending(tmp_path):
    # polyrob owner settle calls settle_payment_request directly on a 'pending' row.
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        assert await invoicing.settle_payment_request(inv["request_id"], db=db) is True
    finally:
        await db.close()


# --- Task 8: free-form payer_contact (payer_hint promoted to first-class) --

@pytest.mark.asyncio
async def test_create_stores_and_returns_payer_contact(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p",
            payer_contact="Alice <a@x.com>", db=db)
        assert inv["payer_contact"] == "Alice <a@x.com>"
        row = await db.fetch_one(
            "SELECT * FROM x402_payment_requests WHERE id = ?", (inv["request_id"],))
        meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        assert meta["payer_contact"] == "Alice <a@x.com>"

        got = await invoicing.get_payment_request(inv["request_id"], db=db)
        assert got["payer_contact"] == "Alice <a@x.com>"

        listed = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert listed[0]["payer_contact"] == "Alice <a@x.com>"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_payer_hint_alias_still_works(tmp_path):
    """The deprecated `payer_hint` kwarg still maps into payer_contact."""
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p",
            payer_hint="Bob <b@x.com>", db=db)
        assert inv["payer_contact"] == "Bob <b@x.com>"
        got = await invoicing.get_payment_request(inv["request_id"], db=db)
        assert got["payer_contact"] == "Bob <b@x.com>"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_payer_contact_takes_precedence_over_payer_hint(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p",
            payer_contact="Alice <a@x.com>", payer_hint="Bob <b@x.com>", db=db)
        assert inv["payer_contact"] == "Alice <a@x.com>"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_no_payer_contact_is_none_no_crash(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        assert inv.get("payer_contact") is None
        got = await invoicing.get_payment_request(inv["request_id"], db=db)
        assert got.get("payer_contact") is None
        listed = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert listed[0].get("payer_contact") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_payer_hint_metadata_read_as_payer_contact(tmp_path):
    """A row written before this change stored only metadata.payer_hint (no
    payer_contact key at all). The read paths (list/get) must surface it as
    payer_contact for back-compat."""
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        legacy_meta = json.dumps({
            "kind": "agent_invoice", "session_id": "s1", "tenant_id": "rob",
            "purpose": "p", "payer_hint": "Legacy Payer", "wake_delivered": False,
            "correspondent_ref": None,
        })
        await db.execute(
            "UPDATE x402_payment_requests SET metadata = ? WHERE id = ?",
            (legacy_meta, inv["request_id"]))

        got = await invoicing.get_payment_request(inv["request_id"], db=db)
        assert got["payer_contact"] == "Legacy Payer"

        listed = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert listed[0]["payer_contact"] == "Legacy Payer"
    finally:
        await db.close()


# --- G-22: non-payment escalation (expiry wake plumbing) --------------------

@pytest.mark.asyncio
async def test_expired_unnotified_and_claim_expiry_wake(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_exp", amount_usd=2.0, purpose="widget",
            expiry_hours=1.0, db=db)
        # not expired yet -> nothing pending
        assert await invoicing.expired_unnotified_invoices(db=db) == []
        await invoicing.expire_stale_requests(db=db, now=inv["expires_at_epoch"] + 10)
        pending = await invoicing.expired_unnotified_invoices(db=db)
        assert len(pending) == 1
        assert pending[0]["request_id"] == inv["request_id"]
        assert pending[0]["session_id"] == "sess_exp"
        assert pending[0]["user_id"] == "rob"
        assert pending[0]["purpose"] == "widget"
        # claim is atomic: first wins, second (racing) claimer loses
        assert await invoicing.claim_expiry_wake(inv["request_id"], db=db) is True
        assert await invoicing.claim_expiry_wake(inv["request_id"], db=db) is False
        assert await invoicing.expired_unnotified_invoices(db=db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_terminal_wake_flags_do_not_cross_settled_and_expired(tmp_path):
    """A row is either settled OR expired (mutually exclusive terminal
    status). expired_unnotified_invoices/settled_unnotified_invoices are each
    status-partitioned, so claiming one row's terminal wake never appears in,
    or is suppressed by, the other query — even though both share the same
    underlying wake_delivered flag."""
    db = await _setup_db(tmp_path)
    try:
        settled_inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s_settled", amount_usd=1.0, purpose="a", db=db)
        expired_inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s_expired", amount_usd=1.0, purpose="b",
            expiry_hours=1.0, db=db)
        await invoicing.settle_payment_request(settled_inv["request_id"], db=db)
        await invoicing.expire_stale_requests(
            db=db, now=expired_inv["expires_at_epoch"] + 10)

        settled_pending = await invoicing.settled_unnotified_invoices(db=db)
        expired_pending = await invoicing.expired_unnotified_invoices(db=db)
        assert [r["request_id"] for r in settled_pending] == [settled_inv["request_id"]]
        assert [r["request_id"] for r in expired_pending] == [expired_inv["request_id"]]

        # claim the settled row's wake -> the expired row's claim is untouched
        assert await invoicing.claim_wake(settled_inv["request_id"], db=db) is True
        assert await invoicing.expired_unnotified_invoices(db=db) == expired_pending
        assert await invoicing.claim_expiry_wake(expired_inv["request_id"], db=db) is True
        assert await invoicing.settled_unnotified_invoices(db=db) == []
        assert await invoicing.expired_unnotified_invoices(db=db) == []
    finally:
        await db.close()


# --- Task 14 review Finding 2: duplicate-renewal TOCTOU ----------------------
# At most one PENDING invoice may exist per subscription_id
# (idx_x402_requests_pending_subscription_unique). create_payment_request must
# treat the resulting sqlite3.IntegrityError as "a pending renewal already
# exists" -- a ValueError, never a crash, never a second row.

@pytest.mark.asyncio
async def test_second_pending_invoice_for_same_subscription_is_refused_not_duplicated(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=10.0, purpose="renewal",
            subscription_id="sub_abc123", db=db)

        with pytest.raises(ValueError, match="pending renewal invoice already exists"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="", amount_usd=10.0, purpose="renewal",
                subscription_id="sub_abc123", db=db)

        rows = await db.fetch_all(
            "SELECT id FROM x402_payment_requests "
            "WHERE json_extract(metadata, '$.subscription_id') = ? AND status = 'pending'",
            ("sub_abc123",))
        assert len(rows) == 1  # exactly one pending renewal invoice survives
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_renewal_creates_for_same_subscription_only_one_wins(tmp_path):
    """Two coroutines racing to create a renewal invoice for the SAME
    subscription (the exact `UVICORN_WORKERS>1` TOCTOU window between
    `subscriptions._has_open_renewal_invoice`'s SELECT and this INSERT) must
    result in exactly ONE pending invoice — the loser gets a clean ValueError,
    never a raw sqlite3.IntegrityError raised to its caller, and never a
    second row."""
    import asyncio

    db = await _setup_db(tmp_path)
    try:
        async def _attempt():
            return await invoicing.create_payment_request(
                user_id="rob", session_id="", amount_usd=10.0, purpose="renewal",
                subscription_id="sub_race", db=db)

        results = await asyncio.gather(_attempt(), _attempt(), return_exceptions=True)

        successes = [r for r in results if isinstance(r, dict)]
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], ValueError)

        rows = await db.fetch_all(
            "SELECT id FROM x402_payment_requests "
            "WHERE json_extract(metadata, '$.subscription_id') = ? AND status = 'pending'",
            ("sub_race",))
        assert len(rows) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_new_renewal_allowed_once_prior_pending_invoice_settles(tmp_path):
    """The unique index is scoped to status='pending' -- once the first
    renewal invoice settles (or expires), a NEW renewal invoice for the same
    subscription must be creatable without hitting the index."""
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=10.0, purpose="renewal",
            subscription_id="sub_cycle", db=db)
        await invoicing.settle_payment_request(first["request_id"], db=db)

        second = await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=10.0, purpose="renewal",
            subscription_id="sub_cycle", db=db)
        assert second["request_id"] != first["request_id"]
    finally:
        await db.close()


# --- Task 14 review Finding 3: invoice-tenant lookup -------------------------

@pytest.mark.asyncio
async def test_get_invoice_tenant_reads_from_metadata(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="tenant_a", session_id="", amount_usd=5.0, purpose="x", db=db)
        assert await invoicing.get_invoice_tenant(inv["request_id"], db=db) == "tenant_a"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_invoice_tenant_missing_row_returns_none(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        assert await invoicing.get_invoice_tenant("req_nope", db=db) is None
        assert await invoicing.get_invoice_tenant("", db=db) is None
    finally:
        await db.close()


# --------------------------------------------------------------------------
# 013 T2 review, Finding 2: x402_invoicing_enabled() was raw-env-only here,
# disagreeing with tools/x402/__init__.py's already-wired mode-aware getter —
# so under autonomous mode with X402_INVOICE_ENABLED unset, the agent could
# create invoices (tool gate ON) that the public settlement/pay endpoints
# (api/x402_endpoints.py, which import THIS function directly) and the
# autonomy-runtime settlement watcher would refuse to serve/settle. This is
# now the shared SSOT tools.x402.x402_invoicing_enabled delegates to.
# --------------------------------------------------------------------------

def _enable_full(monkeypatch):
    """Copied from tests/unit/agents/task/test_autonomy_mode.py."""
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    from agents.task import constants
    constants.reset_autonomy_mode_warnings()


def test_invoicing_enabled_off_supervised_default(monkeypatch):
    """(a) supervised/unset -> disabled exactly as today."""
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    monkeypatch.delenv("X402_INVOICE_ENABLED", raising=False)
    assert invoicing.x402_invoicing_enabled() is False


def test_invoicing_enabled_on_under_autonomous_mode(monkeypatch):
    """(b) effective autonomous mode -> default flips ON."""
    _enable_full(monkeypatch)
    monkeypatch.delenv("X402_INVOICE_ENABLED", raising=False)
    assert invoicing.x402_invoicing_enabled() is True


def test_invoicing_enabled_explicit_false_wins_over_mode(monkeypatch):
    """(c) explicit env false wins over the mode default."""
    _enable_full(monkeypatch)
    monkeypatch.setenv("X402_INVOICE_ENABLED", "false")
    assert invoicing.x402_invoicing_enabled() is False


def test_tools_x402_delegates_to_shared_invoicing_ssot(monkeypatch):
    """tools/x402/__init__.py must delegate to (not duplicate) this resolver so
    the tool-registration gate can never disagree with the endpoint/watcher
    gates — all three read the identical result."""
    import tools.x402 as tools_x402

    _enable_full(monkeypatch)
    monkeypatch.delenv("X402_INVOICE_ENABLED", raising=False)
    assert tools_x402.x402_invoicing_enabled() == invoicing.x402_invoicing_enabled() is True

    monkeypatch.setenv("X402_INVOICE_ENABLED", "false")
    assert tools_x402.x402_invoicing_enabled() == invoicing.x402_invoicing_enabled() is False
