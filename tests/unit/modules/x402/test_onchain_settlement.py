"""Task 11 (Phase 2) — on-chain USDC settlement detection: checkpoint,
amount-based matcher, and amount-collision jitter, at the `invoicing.py`
layer (DB-backed, real schema — same pattern test_invoicing.py uses)."""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing

TREASURY = "0xtreasury"


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_SETTLE_ONCHAIN_DETECT", "X402_INVOICE_AMOUNT_JITTER"):
        monkeypatch.delenv(var, raising=False)


# --- settlement_scan checkpoint --------------------------------------------

@pytest.mark.asyncio
async def test_checkpoint_starts_unset(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        assert await invoicing.get_scan_checkpoint(TREASURY, db=db) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_checkpoint_advances_and_resumes(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await invoicing.advance_scan_checkpoint(TREASURY, 1000, db=db)
        assert await invoicing.get_scan_checkpoint(TREASURY, db=db) == 1000
        await invoicing.advance_scan_checkpoint(TREASURY, 1500, db=db)
        assert await invoicing.get_scan_checkpoint(TREASURY, db=db) == 1500
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_checkpoint_never_regresses(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await invoicing.advance_scan_checkpoint(TREASURY, 1000, db=db)
        # a stray out-of-order/concurrent call with a SMALLER block must not
        # roll the checkpoint backward
        await invoicing.advance_scan_checkpoint(TREASURY, 500, db=db)
        assert await invoicing.get_scan_checkpoint(TREASURY, db=db) == 1000
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_checkpoint_is_per_treasury(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await invoicing.advance_scan_checkpoint("0xtreasuryA", 100, db=db)
        await invoicing.advance_scan_checkpoint("0xtreasuryB", 200, db=db)
        assert await invoicing.get_scan_checkpoint("0xtreasuryA", db=db) == 100
        assert await invoicing.get_scan_checkpoint("0xtreasuryB", db=db) == 200
    finally:
        await db.close()


# --- amount-based pending-invoice matcher -----------------------------------

@pytest.mark.asyncio
async def test_match_finds_exact_amount_pending_invoice(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=12.34, purpose="p", db=db)
        match = await invoicing.match_pending_invoice_by_amount(12.34, TREASURY, db=db)
        assert match is not None
        assert match["request_id"] == inv["request_id"]
        assert match["session_id"] == "s1"
        assert match["user_id"] == "rob"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_match_returns_none_for_no_amount_match(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=12.34, purpose="p", db=db)
        assert await invoicing.match_pending_invoice_by_amount(99.99, TREASURY, db=db) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_match_ignores_non_pending_rows(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        assert await invoicing.match_pending_invoice_by_amount(5.0, TREASURY, db=db) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_match_oldest_first_on_equal_amount_collision(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        older = await invoicing.create_payment_request(
            user_id="rob", session_id="s_old", amount_usd=5.0, purpose="a", db=db)
        newer = await invoicing.create_payment_request(
            user_id="rob", session_id="s_new", amount_usd=5.0, purpose="b", db=db)
        match = await invoicing.match_pending_invoice_by_amount(5.0, TREASURY, db=db)
        assert match["request_id"] == older["request_id"]
        assert match["request_id"] != newer["request_id"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_match_is_treasury_scoped(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        assert await invoicing.match_pending_invoice_by_amount(
            5.0, "0xsome-other-treasury", db=db) is None
    finally:
        await db.close()


# --- amount-collision jitter -------------------------------------------------

@pytest.mark.asyncio
async def test_jitter_inert_when_detection_disabled(tmp_path, monkeypatch):
    # X402_INVOICE_AMOUNT_JITTER defaults ON, but detection defaults OFF ->
    # the jitter must not fire; two same-amount invoices both keep the exact
    # amount (byte-identical to pre-T11 behavior).
    monkeypatch.delenv("X402_SETTLE_ONCHAIN_DETECT", raising=False)
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=7.5, purpose="a", db=db)
        second = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=7.5, purpose="b", db=db)
        assert first["amount_usd"] == 7.5
        assert second["amount_usd"] == 7.5
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_jitter_nudges_colliding_amount_when_detection_on(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "true")
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=7.5, purpose="a", db=db)
        second = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=7.5, purpose="b", db=db)
        assert first["amount_usd"] == 7.5
        assert second["amount_usd"] != 7.5
        assert round(second["amount_usd"] - 7.5, 6) == 0.0001
        # reflected in the stored row too (what "instructions" read from)
        row = await db.fetch_one(
            "SELECT amount_usd FROM x402_payment_requests WHERE id = ?",
            (second["request_id"],))
        assert row["amount_usd"] == second["amount_usd"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_jitter_flag_off_is_overridden_when_detection_on(tmp_path, monkeypatch):
    """I2 safety fix (Task 11 review): X402_SETTLE_ONCHAIN_DETECT=true +
    X402_INVOICE_AMOUNT_JITTER=false is an UNSAFE combination — on-chain
    auto-settlement with zero disambiguation. Jitter is forced on internally
    regardless of the flag; the flag can only disable jitter while detection
    is also off (where it stays fully inert — see
    test_jitter_inert_when_detection_disabled)."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "false")
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=3.0, purpose="a", db=db)
        second = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=3.0, purpose="b", db=db)
        assert first["amount_usd"] == 3.0
        assert second["amount_usd"] != 3.0  # forced jitter, flag notwithstanding
        assert round(second["amount_usd"] - 3.0, 6) == 0.0001
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_jitter_forced_on_logs_a_notice(tmp_path, monkeypatch, caplog):
    """I2: forcing jitter on against the operator's explicit setting must be
    observable (a logged notice), not a silent override."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "false")
    db = await _setup_db(tmp_path)
    try:
        with caplog.at_level("WARNING", logger="modules.x402.invoicing"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s1", amount_usd=1.0, purpose="a", db=db)
        assert any("forcing" in r.message.lower() and "jitter" in r.message.lower()
                   for r in caplog.records)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_jitter_flag_off_stays_inert_when_detection_also_off(tmp_path, monkeypatch):
    """The flag retains its ONE remaining function: disabling jitter is only
    honored while detection is off (where it was already inert either way)."""
    monkeypatch.delenv("X402_SETTLE_ONCHAIN_DETECT", raising=False)
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "false")
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=3.0, purpose="a", db=db)
        second = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=3.0, purpose="b", db=db)
        assert first["amount_usd"] == second["amount_usd"] == 3.0
    finally:
        await db.close()


# --- I1: concurrent-create TOCTOU (amount-dedupe race) -----------------------
#
# `DatabaseConnection` wraps a SYNCHRONOUS `sqlite3.Connection` — none of
# `execute`/`fetch_one`/`fetch_all` ever hit a real I/O suspension point, and
# `asyncio.Lock.acquire()` returns immediately when uncontended (no genuine
# yield either). So a bare `asyncio.gather(coro1, coro2)` never actually
# interleaves `coro1`/`coro2`: the first-scheduled task runs to completion
# (or to its first REAL suspension) before the second one gets a turn. That
# means a naive version of this test would pass identically whether or not
# the `_treasury_lock` existed — there is no genuine check-then-act race for
# it to close, so the test would never catch a regression that removed the
# lock. Forcing a real yield point (`await asyncio.sleep(0)`) right after the
# collision-check SELECT and before the INSERT reintroduces a genuine
# scheduler switch at exactly the TOCTOU window `_treasury_lock` guards —
# WITHOUT the lock the second task's SELECT can now run before the first
# task's INSERT (both see "no collision"); WITH the lock, the second task
# blocks trying to acquire it (a real suspension) until the first releases.


@pytest.mark.asyncio
async def test_concurrent_creates_same_amount_end_with_distinct_amounts(tmp_path, monkeypatch):
    """I1 fix: the collision-check SELECT and the INSERT in
    `create_payment_request` are two separate awaited DB calls — without
    serialization, two concurrent creates for the same treasury+amount can
    both observe "no collision" before either has inserted, so BOTH keep the
    exact amount (defeating the jitter this is all for). The per-treasury
    `asyncio.Lock` must close that window.

    A forced yield point is monkeypatched into `_dedupe_amount_for_treasury`
    (see module-level comment above) so the two concurrent creates actually
    interleave at the check-then-act boundary — without this, `asyncio.gather`
    over two sync-sqlite-backed coroutines never truly interleaves, and this
    test would pass identically with the lock removed (proven below the
    assertions, and independently verified by hand: temporarily bypassing
    `_treasury_lock` in the source reproduces the failure this test now
    catches)."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "true")

    import asyncio

    # Force a genuine scheduler switch between the collision-check SELECT and
    # the caller's subsequent INSERT — the exact window `_treasury_lock` is
    # meant to serialize. Without this, nothing in the sync-sqlite call chain
    # ever actually suspends, so `asyncio.gather` runs the two creates
    # sequentially in disguise and the lock is never truly exercised.
    orig_dedupe = invoicing._dedupe_amount_for_treasury

    async def _dedupe_then_yield(*args, **kwargs):
        result = await orig_dedupe(*args, **kwargs)
        await asyncio.sleep(0)  # real suspension point: check-then-act gap
        return result

    monkeypatch.setattr(invoicing, "_dedupe_amount_for_treasury", _dedupe_then_yield)

    db = await _setup_db(tmp_path)
    try:
        results = await asyncio.gather(
            invoicing.create_payment_request(
                user_id="rob", session_id="s1", amount_usd=9.0, purpose="a", db=db),
            invoicing.create_payment_request(
                user_id="rob", session_id="s2", amount_usd=9.0, purpose="b", db=db),
        )
        amounts = {r["amount_usd"] for r in results}
        assert len(amounts) == 2, (
            f"both concurrent creates ended with the SAME amount {results!r} — "
            "the TOCTOU race defeated the jitter")
        assert 9.0 in amounts

        # user_id may be stored NULL (FK-fallback; tenant lives in
        # metadata.tenant_id) — match on that instead of the column.
        rows = await db.fetch_all(
            "SELECT amount_usd FROM x402_payment_requests "
            "WHERE json_extract(metadata, '$.tenant_id') = 'rob'")
        row_amounts = {r["amount_usd"] for r in rows}
        assert row_amounts == amounts  # persisted amounts match what was returned
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_creates_distinct_amounts_are_unaffected_by_the_lock(
        tmp_path, monkeypatch):
    """The per-treasury lock serializes access but must never perturb an
    amount that has no collision to begin with — two concurrent creates at
    DIFFERENT amounts both keep their exact amount."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "true")
    db = await _setup_db(tmp_path)
    try:
        import asyncio

        results = await asyncio.gather(
            invoicing.create_payment_request(
                user_id="rob", session_id="s1", amount_usd=11.0, purpose="a", db=db),
            invoicing.create_payment_request(
                user_id="rob", session_id="s2", amount_usd=22.0, purpose="b", db=db),
        )
        amounts = {r["amount_usd"] for r in results}
        assert amounts == {11.0, 22.0}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_jitter_never_exceeds_cap(tmp_path, monkeypatch):
    """M5 change: when jitter cannot find a unique amount under a (tight) cap,
    the create now FAILS CLOSED (raises) rather than silently producing a
    second same-amount pending invoice — the partial UNIQUE index forbids the
    ambiguous duplicate, and jitter must never exceed the cap. (With a normal
    cap, jitter has room and the second invoice succeeds at 10.0001 — see
    test_two_same_amount_invoices_get_distinct_jittered_amounts.)"""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "true")
    monkeypatch.setenv("X402_INVOICE_MAX_USD", "10.00005")
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=10.0, purpose="a", db=db)
        assert first["amount_usd"] == 10.0
        # 10.0 collides; 10.0001 > cap -> no unique candidate -> refuse.
        with pytest.raises(ValueError):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s2", amount_usd=10.0, purpose="b", db=db)
        # And the invariant holds: no pending invoice over the cap was created.
        rows = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert all(r["amount_usd"] <= 10.00005 for r in rows)
        assert len([r for r in rows if r["status"] == "pending"]) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_jitter_only_compares_against_pending_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "true")
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="a", db=db)
        await invoicing.settle_payment_request(first["request_id"], db=db)
        # first is now 'completed' — a fresh invoice at the same amount has
        # no PENDING collision to disambiguate, so it keeps the exact amount
        second = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=4.0, purpose="b", db=db)
        assert second["amount_usd"] == 4.0
    finally:
        await db.close()
