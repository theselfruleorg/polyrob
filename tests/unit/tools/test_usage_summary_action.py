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
