"""Post-review Fix 2 — enable-then-disable jitter-index residue.

`_ensure_pending_amount_unique_index` (the M5 partial UNIQUE index on
``(recipient, amount_usd)`` for pending agent invoices) is created ONLY on the
jitter-active path (``X402_SETTLE_ONCHAIN_DETECT=true``) and is never dropped
when detection is later disabled again (self-healing ``CREATE ... IF NOT
EXISTS``, no matching DROP). A deployment that enabled detection once, then
disabled it, can still hit that RESIDUAL index on the no-jitter else-path
insert in ``create_payment_request`` — before the fix this escaped as a raw
``sqlite3.IntegrityError``; the fix converts it into the same clean
``ValueError`` refusal the jitter-cap path already uses.
"""
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
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_INVOICE_AMOUNT_JITTER"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_residual_index_refused_cleanly_after_detection_disabled(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        # 1) Detection ON — this creates the M5 partial-unique index and mints
        #    the first invoice at amount 9.0 (no collision yet).
        monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=9.0, purpose="a", db=db)
        assert first["amount_usd"] == 9.0
        idx = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='index' AND name = ?",
            (invoicing._PENDING_AMOUNT_INDEX,))
        assert len(idx) == 1  # index now exists

        # 2) Detection OFF again — the index is NOT dropped, so a same-amount
        #    pending invoice for the same treasury hits the residual index on
        #    the no-jitter else-path insert. Must fail CLEANLY (ValueError),
        #    never a raw sqlite3.IntegrityError.
        monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "false")
        with pytest.raises(ValueError, match="already exists"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s2", amount_usd=9.0, purpose="b", db=db)

        # Only the first invoice landed.
        rows = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert len(rows) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_residual_index_same_amount_still_allowed(tmp_path, monkeypatch):
    """Sanity: when the index was NEVER created (detection never enabled), a
    same-amount pending duplicate is still the intentional legacy allowance —
    unaffected by the Fix 2 refusal wrapper."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "false")
    db = await _setup_db(tmp_path)
    try:
        a = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=4.0, purpose="a", db=db)
        b = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=4.0, purpose="b", db=db)
        assert a["amount_usd"] == 4.0 and b["amount_usd"] == 4.0
    finally:
        await db.close()
