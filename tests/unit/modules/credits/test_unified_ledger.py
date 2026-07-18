"""Money loop — unified ledger: one read model over costs / wallet spend / receipts."""
import pytest

from modules.credits.unified_ledger import build_ledger, format_ledger
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, session_id TEXT, resource_type TEXT,
            cost REAL, input_tokens INTEGER, output_tokens INTEGER,
            cached_tokens INTEGER, api_cost_usd REAL, markup_multiplier REAL,
            metadata TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
    return db


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    # point the telemetry event log at a scratch DB so the wallet leg is hermetic
    from agents.task.telemetry import event_log as el
    monkeypatch.setattr(el, "_INSTANCES", {}, raising=False)


@pytest.mark.asyncio
async def test_ledger_joins_costs_and_receipts(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost, "
            "api_cost_usd) VALUES ('rob', 's1', 'llm_call', 10.0, 0.05)")
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="report", db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=2.0, purpose="pending one", db=db)

        ledger = await build_ledger("rob", days=7, db=db)
        assert ledger["llm_api_cost_usd"] == pytest.approx(0.05)
        assert ledger["credits_spent"] == pytest.approx(10.0)
        assert ledger["llm_calls"] == 1
        assert ledger["treasury"]["income_usd"] == pytest.approx(5.0)
        assert ledger["settled_payments"] == 1
        assert ledger["pending_invoices"] == 1
        assert ledger["pending_invoices_usd"] == pytest.approx(2.0)
        # treasury.net_usd = income - spend, NEVER minus the runtime api_cost
        # (that merge was the 2026-07-16 bug this ledger split fixes).
        assert ledger["treasury"]["net_usd"] == pytest.approx(5.0 - ledger["wallet_spend_usd"])

        text = format_ledger(ledger)
        # format_ledger (Task 3) renders income/spend, never "earned" (H14b
        # honesty rule: never present treasury via the retired legacy word).
        assert "income" in text and "$5.0000" in text and "net" in text
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ledger_tenant_scoped(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="other", session_id="s9", amount_usd=9.0, purpose="theirs", db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        ledger = await build_ledger("rob", days=7, db=db)
        assert ledger["treasury"]["income_usd"] == 0.0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ledger_tenant_match_does_not_wildcard_leak(tmp_path):
    """G-14: the inbound leg's tenant match used `metadata LIKE '%"tenant_id":
    "<id>"%'` — SQLite LIKE treats `_`/`%` as wildcards, and real tenant ids
    contain underscores (u_<hex>). The settled ROW belongs to the non-
    underscore lookalike tenant 'uXabc'; the ledger QUERY runs as 'u_abc' — the
    '_' in the QUERYING id is what the old LIKE pattern read as a live wildcard
    (matching 'X'), so 'u_abc' would incorrectly read uXabc's earned/pending
    totals on this money query."""
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="uXabc", session_id="s1", amount_usd=5.0, purpose="theirs", db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        leaked = await build_ledger("u_abc", days=7, db=db)
        assert leaked["treasury"]["income_usd"] == 0.0
        assert leaked["settled_payments"] == 0
        theirs = await build_ledger("uXabc", days=7, db=db)
        assert theirs["treasury"]["income_usd"] == pytest.approx(5.0)
        assert theirs["settled_payments"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ledger_fail_open_without_db():
    ledger = await build_ledger("rob", days=7, db=None) if False else None
    # direct call with a broken db object: every leg degrades to zeros
    class _Broken:
        async def fetch_one(self, *a, **k):
            raise RuntimeError("nope")
        async def fetch_all(self, *a, **k):
            raise RuntimeError("nope")
    ledger = await build_ledger("rob", days=7, db=_Broken())
    assert ledger["treasury"]["income_usd"] == 0.0 and ledger["llm_api_cost_usd"] == 0.0
    assert "net_usd" in ledger["treasury"]


@pytest.mark.asyncio
async def test_empty_tenant_refused():
    # query(user_id=None) means "no filter" in the event log — an empty tenant
    # must never widen into the platform-wide spend aggregate.
    with pytest.raises(ValueError, match="authenticated tenant"):
        await build_ledger("", days=7, db=None)
