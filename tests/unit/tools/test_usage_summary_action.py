"""Task 13 (Phase 3 R3) — `usage_summary` agent action: tenant-scoped usage
rollup + a non-binding suggested-invoice draft. Gated USAGE_INVOICE_BRIDGE_ENABLED
(default OFF; deliberately NOT in _SAFE_LOCAL_FLAGS). Never creates a payment
request — the agent must fire the separate, approval-gated x402_request itself.
"""
import logging

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.registry.service import Registry
from tools.controller.service import Controller


def _controller(user_id="tenant-A"):
    # mirrors tests/unit/tools/test_agent_status_action.py::_controller
    c = object.__new__(Controller)
    c.logger = logging.getLogger("usage-summary-test")
    c.registry = Registry()
    c.user_id = user_id
    c.session_id = "s1"
    return c


def _live_controller(monkeypatch, user_id="tenant-A"):
    monkeypatch.setenv("USAGE_INVOICE_BRIDGE_ENABLED", "true")
    c = _controller(user_id=user_id)
    c._register_usage_summary_action()
    return c


# --- registration gating -----------------------------------------------------

def test_usage_summary_registered_when_flag_on(monkeypatch):
    monkeypatch.setenv("USAGE_INVOICE_BRIDGE_ENABLED", "true")
    c = _controller()
    c._register_usage_summary_action()
    assert "usage_summary" in c.registry.registry.actions


def test_usage_summary_absent_when_flag_off(monkeypatch):
    monkeypatch.setenv("USAGE_INVOICE_BRIDGE_ENABLED", "false")
    c = _controller()
    c._register_usage_summary_action()
    assert "usage_summary" not in c.registry.registry.actions


def test_usage_summary_absent_by_default_on_server(monkeypatch):
    monkeypatch.delenv("USAGE_INVOICE_BRIDGE_ENABLED", raising=False)
    c = _controller()
    c._register_usage_summary_action()
    assert "usage_summary" not in c.registry.registry.actions


def test_usage_summary_absent_by_default_even_under_polyrob_local(monkeypatch):
    # Deliberately NOT in _SAFE_LOCAL_FLAGS — an explicit billing feature must
    # stay OFF even in local mode unless the operator opts in explicitly.
    monkeypatch.delenv("USAGE_INVOICE_BRIDGE_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    c = _controller()
    c._register_usage_summary_action()
    assert "usage_summary" not in c.registry.registry.actions


# --- behavior ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_usage_summary_anon_refused(monkeypatch):
    c = _live_controller(monkeypatch, user_id=None)
    action = c.registry.registry.actions["usage_summary"]

    class _AnonCtx:
        user_id = ""

    result = await action.function(action.param_model(), execution_context=_AnonCtx())
    assert result.error and "authenticated tenant" in result.error


@pytest.mark.asyncio
async def test_usage_summary_reports_tenant_rollup(monkeypatch):
    c = _live_controller(monkeypatch, user_id="tenant-A")

    async def _fake_rollup(user_id, *, session_id=None, since=None, db=None):
        assert user_id == "tenant-A"
        return {"user_id": user_id, "session_id": session_id, "since": since,
                "api_cost_usd": 2.0, "credits": 200.0, "calls": 4}

    import modules.credits.usage_rollup as ur
    monkeypatch.setattr(ur, "usage_rollup", _fake_rollup)

    action = c.registry.registry.actions["usage_summary"]
    result = await action.function(action.param_model(), execution_context=None)
    text = result.extracted_content
    assert "api_cost_usd: $2.0000" in text
    assert "credits:      200.00" in text
    assert "calls:        4" in text
    assert "Suggested invoice" in text
    assert result.metadata["rollup"]["api_cost_usd"] == 2.0
    assert result.metadata["suggested_invoice"]["amount_usd"] == pytest.approx(2.0)
    assert result.error is None


@pytest.mark.asyncio
async def test_usage_summary_tenant_from_execution_context(monkeypatch):
    c = _live_controller(monkeypatch, user_id="controller-tenant")
    seen = {}

    async def _fake_rollup(user_id, *, session_id=None, since=None, db=None):
        seen["user_id"] = user_id
        return {"user_id": user_id, "session_id": session_id, "since": since,
                "api_cost_usd": 0.0, "credits": 0.0, "calls": 0}

    import modules.credits.usage_rollup as ur
    monkeypatch.setattr(ur, "usage_rollup", _fake_rollup)

    class Ctx:
        user_id = "ctx-tenant"

    action = c.registry.registry.actions["usage_summary"]
    await action.function(action.param_model(), execution_context=Ctx())
    assert seen["user_id"] == "ctx-tenant"  # execution_context wins over self.user_id


@pytest.mark.asyncio
async def test_usage_summary_passes_session_and_since_filters(monkeypatch):
    c = _live_controller(monkeypatch)
    seen = {}

    async def _fake_rollup(user_id, *, session_id=None, since=None, db=None):
        seen["session_id"] = session_id
        seen["since"] = since
        return {"user_id": user_id, "session_id": session_id, "since": since,
                "api_cost_usd": 0.5, "credits": 50.0, "calls": 1}

    import modules.credits.usage_rollup as ur
    monkeypatch.setattr(ur, "usage_rollup", _fake_rollup)

    action = c.registry.registry.actions["usage_summary"]
    params = action.param_model(session_id="s9", since="2026-07-01")
    result = await action.function(params, execution_context=None)
    assert seen["session_id"] == "s9"
    assert seen["since"] == "2026-07-01"
    assert "session s9" in result.extracted_content
    assert "since 2026-07-01" in result.extracted_content


@pytest.mark.asyncio
async def test_usage_summary_over_cap_draft_is_flagged_in_output(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_MAX_USD", "1.00")
    c = _live_controller(monkeypatch)

    async def _fake_rollup(user_id, *, session_id=None, since=None, db=None):
        return {"user_id": user_id, "session_id": session_id, "since": since,
                "api_cost_usd": 5.0, "credits": 500.0, "calls": 1}

    import modules.credits.usage_rollup as ur
    monkeypatch.setattr(ur, "usage_rollup", _fake_rollup)

    action = c.registry.registry.actions["usage_summary"]
    result = await action.function(action.param_model(), execution_context=None)
    assert "WARNING" in result.extracted_content
    assert result.metadata["suggested_invoice"]["over_cap"] is True
    assert result.metadata["suggested_invoice"]["amount_usd"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_usage_summary_never_creates_a_payment_request(monkeypatch):
    """The bridge NEVER auto-sends money — spy create_payment_request."""
    c = _live_controller(monkeypatch)

    async def _fake_rollup(user_id, *, session_id=None, since=None, db=None):
        return {"user_id": user_id, "session_id": session_id, "since": since,
                "api_cost_usd": 3.0, "credits": 300.0, "calls": 2}

    import modules.credits.usage_rollup as ur
    monkeypatch.setattr(ur, "usage_rollup", _fake_rollup)

    import modules.x402.invoicing as inv
    called = {"v": False}

    async def _spy(*a, **k):
        called["v"] = True
        raise AssertionError("usage_summary must never call create_payment_request")

    monkeypatch.setattr(inv, "create_payment_request", _spy)

    action = c.registry.registry.actions["usage_summary"]
    result = await action.function(action.param_model(), execution_context=None)
    assert called["v"] is False
    assert result.error is None


@pytest.mark.asyncio
async def test_an_unreadable_rollup_is_an_error_not_zeros(monkeypatch):
    c = _live_controller(monkeypatch, user_id="tenant-A")

    async def _fake_rollup(user_id, *, session_id=None, since=None, db=None):
        return {"user_id": user_id, "session_id": None, "since": None,
                "api_cost_usd": 0.0, "credits": 0.0, "calls": 0,
                "unreadable": "OperationalError: database is locked"}

    import modules.credits.usage_rollup as ur
    monkeypatch.setattr(ur, "usage_rollup", _fake_rollup)
    action = c.registry.registry.actions["usage_summary"]
    result = await action.function(action.param_model(), execution_context=None)
    assert result.error and "could not be read (OperationalError: database is locked)" in result.error
    assert "This is not $0" in result.error
    assert not result.extracted_content


# --- 120 W3: the owner's cost breakdown, on every posture ------------------------

def _owner_ledger(tmp_path, monkeypatch, cost=0.42):
    import sqlite3
    db = tmp_path / "database" / "bot.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage_records (id INTEGER PRIMARY KEY, user_id TEXT, "
                "session_id TEXT, input_tokens INTEGER, cached_tokens INTEGER, "
                "output_tokens INTEGER, api_cost_usd REAL, metadata TEXT, timestamp TEXT)")
    con.execute("INSERT INTO usage_records (user_id, session_id, input_tokens, cached_tokens, "
                "output_tokens, api_cost_usd, metadata, timestamp) VALUES "
                "('owner','s1',100,50,5,?,'{}',strftime('%Y-%m-%d %H:%M:%S','now'))", (cost,))
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))


@pytest.fixture
def _owner_is(monkeypatch):
    import core.instance as inst
    monkeypatch.setattr(inst, "is_owner_uid", lambda uid, **k: str(uid) == "owner")


def _owner_controller(monkeypatch, flag="false"):
    monkeypatch.setenv("USAGE_INVOICE_BRIDGE_ENABLED", flag)
    c = _controller(user_id="owner")
    c._register_usage_summary_action()
    return c


class _Owner:
    user_id = "owner"


def test_the_owner_tenant_has_usage_summary_with_the_flag_off(monkeypatch, _owner_is):
    c = _owner_controller(monkeypatch)
    assert "usage_summary" in c.registry.registry.actions


@pytest.mark.asyncio
async def test_the_breakdown_is_the_usage_verb_text(tmp_path, monkeypatch, _owner_is):
    _owner_ledger(tmp_path, monkeypatch)
    import cron.digest
    monkeypatch.setattr(cron.digest, "rail_names", lambda *a, **k: {})
    c = _owner_controller(monkeypatch)
    action = c.registry.registry.actions["usage_summary"]
    result = await action.function(action.param_model(days=1), execution_context=_Owner())
    from surfaces.telegram.usage_ops import usage_reply
    assert result.error is None
    assert result.extracted_content == usage_reply("owner", str(tmp_path), ["1"])
    assert "AI cost, the last 24 h: $0.42" in result.extracted_content
    assert "Suggested invoice" not in result.extracted_content   # the draft stays gated


@pytest.mark.asyncio
async def test_days_default_and_clamp(tmp_path, monkeypatch, _owner_is):
    _owner_ledger(tmp_path, monkeypatch)
    c = _owner_controller(monkeypatch)
    action = c.registry.registry.actions["usage_summary"]
    text = (await action.function(action.param_model(), execution_context=_Owner())).extracted_content
    assert "the last 7 days" in text
    text = (await action.function(action.param_model(days=500),
                                  execution_context=_Owner())).extracted_content
    assert "the last 90 days" in text


@pytest.mark.asyncio
async def test_owner_with_the_bridge_on_gets_both_parts(tmp_path, monkeypatch, _owner_is):
    _owner_ledger(tmp_path, monkeypatch)

    async def _fake_rollup(user_id, *, session_id=None, since=None, db=None):
        return {"user_id": user_id, "session_id": session_id, "since": since,
                "api_cost_usd": 0.42, "credits": 42.0, "calls": 1}

    import modules.credits.usage_rollup as ur
    monkeypatch.setattr(ur, "usage_rollup", _fake_rollup)
    c = _owner_controller(monkeypatch, flag="true")
    action = c.registry.registry.actions["usage_summary"]
    result = await action.function(action.param_model(), execution_context=_Owner())
    text = result.extracted_content
    assert text.index("AI cost, the last 7 days") < text.index("Suggested invoice")
    assert result.metadata["rollup"]["calls"] == 1


@pytest.mark.asyncio
async def test_an_unreadable_ledger_is_named_not_zero(tmp_path, monkeypatch, _owner_is):
    bad = tmp_path / "bot.db"
    bad.write_text("not sqlite")
    monkeypatch.setenv("DB_PATH", str(bad))
    c = _owner_controller(monkeypatch)
    action = c.registry.registry.actions["usage_summary"]
    text = (await action.function(action.param_model(), execution_context=_Owner())).extracted_content
    assert "could not read the records" in text and "not a zero" in text


@pytest.mark.asyncio
async def test_an_unreadable_run_index_is_named(tmp_path, monkeypatch, _owner_is):
    _owner_ledger(tmp_path, monkeypatch)
    (tmp_path / "memory.db").write_text("not sqlite")
    c = _owner_controller(monkeypatch)
    action = c.registry.registry.actions["usage_summary"]
    text = (await action.function(action.param_model(days=1),
                                  execution_context=_Owner())).extracted_content
    assert "could not read the run records" in text and "not zero" in text


@pytest.mark.asyncio
async def test_a_tenant_turn_on_an_owner_controller_without_the_bridge_is_refused(
        tmp_path, monkeypatch, _owner_is):
    c = _owner_controller(monkeypatch)
    action = c.registry.registry.actions["usage_summary"]

    class _Other:
        user_id = "tenant-b"

    result = await action.function(action.param_model(), execution_context=_Other())
    assert result.error and "the cost breakdown is the owner's" in result.error
