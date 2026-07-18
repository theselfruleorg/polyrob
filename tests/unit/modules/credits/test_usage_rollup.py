"""Tenant-scoped usage rollup — the metering->invoice bridge (Task 13, Phase 3 R3).

Regression coverage for G-29: `LLMUsageTracker.get_session_breakdown` aggregates
by session_id ALONE (no user_id filter) — a cross-tenant read hole. `usage_rollup`
is the honest, tenant-scoped replacement new callers use.
"""
import pytest

from modules.credits.usage_rollup import (
    build_invoice_draft,
    usage_invoice_bridge_enabled,
    usage_invoice_markup,
    usage_rollup,
)
from modules.database.connection import DatabaseConnection


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, session_id TEXT, resource_type TEXT,
            cost REAL, input_tokens INTEGER, output_tokens INTEGER,
            cached_tokens INTEGER, api_cost_usd REAL, markup_multiplier REAL,
            metadata TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
    return db


async def _insert(db, user_id, session_id, api_cost_usd, cost=1.0, timestamp=None):
    if timestamp:
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost, "
            "api_cost_usd, timestamp) VALUES (?, ?, 'llm_call', ?, ?, ?)",
            (user_id, session_id, cost, api_cost_usd, timestamp))
    else:
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost, "
            "api_cost_usd) VALUES (?, ?, 'llm_call', ?, ?)",
            (user_id, session_id, cost, api_cost_usd))


# --- usage_rollup --------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_scoped_no_cross_tenant_leak(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await _insert(db, "rob", "s1", 0.10)
        await _insert(db, "other", "s2", 5.00)

        rob = await usage_rollup("rob", db=db)
        other = await usage_rollup("other", db=db)

        assert rob["api_cost_usd"] == pytest.approx(0.10)
        assert rob["calls"] == 1
        assert other["api_cost_usd"] == pytest.approx(5.00)
        assert other["calls"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_session_filter_narrows(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await _insert(db, "rob", "s1", 0.10)
        await _insert(db, "rob", "s2", 0.20)

        narrowed = await usage_rollup("rob", session_id="s1", db=db)
        assert narrowed["api_cost_usd"] == pytest.approx(0.10)
        assert narrowed["calls"] == 1
        assert "by_session" not in narrowed  # only computed when session_id absent

        total = await usage_rollup("rob", db=db)
        assert total["api_cost_usd"] == pytest.approx(0.30)
        assert total["calls"] == 2
        assert {r["session_id"] for r in total["by_session"]} == {"s1", "s2"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_since_filter_narrows(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await _insert(db, "rob", "s1", 0.10, timestamp="2020-01-01 00:00:00")
        await _insert(db, "rob", "s1", 0.20, timestamp="2030-01-01 00:00:00")

        recent = await usage_rollup("rob", since="2025-01-01", db=db)
        assert recent["api_cost_usd"] == pytest.approx(0.20)
        assert recent["calls"] == 1

        everything = await usage_rollup("rob", db=db)
        assert everything["api_cost_usd"] == pytest.approx(0.30)
        assert everything["calls"] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_error_fails_open_to_zeros():
    class _Broken:
        async def fetch_one(self, *a, **k):
            raise RuntimeError("nope")

        async def fetch_all(self, *a, **k):
            raise RuntimeError("nope")

    rollup = await usage_rollup("rob", db=_Broken())
    assert rollup["api_cost_usd"] == 0.0
    assert rollup["credits"] == 0.0
    assert rollup["calls"] == 0
    assert "by_session" not in rollup


@pytest.mark.asyncio
async def test_empty_tenant_never_widens_to_all_tenants(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await _insert(db, "rob", "s1", 1.0)
        rollup = await usage_rollup("", db=db)
        assert rollup["api_cost_usd"] == 0.0
        assert rollup["calls"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_db_service_fails_open(monkeypatch):
    from core.container import DependencyContainer

    class _NoServiceContainer:
        def get_service(self, name):
            return None

    monkeypatch.setattr(DependencyContainer, "get_instance", lambda: _NoServiceContainer())
    rollup = await usage_rollup("rob")
    assert rollup["api_cost_usd"] == 0.0
    assert rollup["calls"] == 0


# --- build_invoice_draft --------------------------------------------------------

def test_draft_amount_passthrough_markup(monkeypatch):
    monkeypatch.delenv("USAGE_INVOICE_MARKUP", raising=False)
    monkeypatch.delenv("X402_INVOICE_MAX_USD", raising=False)
    rollup = {"api_cost_usd": 2.00, "session_id": None, "since": None}
    draft = build_invoice_draft(rollup)
    assert draft["amount_usd"] == pytest.approx(2.00)
    assert draft["markup"] == 1.0
    assert draft["purpose"] == "usage all-time summary"
    assert draft["over_cap"] is False


def test_draft_applies_configured_markup(monkeypatch):
    monkeypatch.setenv("USAGE_INVOICE_MARKUP", "1.5")
    rollup = {"api_cost_usd": 2.00, "session_id": None, "since": None}
    draft = build_invoice_draft(rollup)
    assert draft["amount_usd"] == pytest.approx(3.00)
    assert draft["markup"] == 1.5
    assert usage_invoice_markup() == 1.5


def test_draft_period_label_uses_session_then_since(monkeypatch):
    rollup = {"api_cost_usd": 1.0, "session_id": "s1", "since": "2026-01-01"}
    assert build_invoice_draft(rollup)["purpose"] == "usage session s1 summary"
    rollup2 = {"api_cost_usd": 1.0, "session_id": None, "since": "2026-01-01"}
    assert build_invoice_draft(rollup2)["purpose"] == "usage since 2026-01-01 summary"


def test_draft_over_cap_is_flagged_not_clamped(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_MAX_USD", "1.00")
    monkeypatch.delenv("USAGE_INVOICE_MARKUP", raising=False)
    rollup = {"api_cost_usd": 5.00, "session_id": None, "since": None}
    draft = build_invoice_draft(rollup)
    assert draft["amount_usd"] == pytest.approx(5.00)  # NOT clamped to 1.00
    assert draft["over_cap"] is True
    assert "exceeds" in draft["note"]


def test_draft_never_creates_a_payment_request(monkeypatch):
    """The bridge NEVER auto-sends money — spy create_payment_request."""
    import modules.x402.invoicing as inv

    spy_called = {"v": False}

    async def _spy(*a, **k):
        spy_called["v"] = True
        raise AssertionError("build_invoice_draft must never call create_payment_request")

    monkeypatch.setattr(inv, "create_payment_request", _spy)
    rollup = {"api_cost_usd": 2.00, "session_id": "s1", "since": None}
    draft = build_invoice_draft(rollup)
    assert spy_called["v"] is False
    assert draft["purpose"] == "usage session s1 summary"
    assert "create_payment_request" not in build_invoice_draft.__code__.co_names


def test_bridge_flag_default_off(monkeypatch):
    monkeypatch.delenv("USAGE_INVOICE_BRIDGE_ENABLED", raising=False)
    assert usage_invoice_bridge_enabled() is False


def test_bridge_flag_on(monkeypatch):
    monkeypatch.setenv("USAGE_INVOICE_BRIDGE_ENABLED", "true")
    assert usage_invoice_bridge_enabled() is True


def test_markup_flag_default_passthrough(monkeypatch):
    monkeypatch.delenv("USAGE_INVOICE_MARKUP", raising=False)
    assert usage_invoice_markup() == 1.0
