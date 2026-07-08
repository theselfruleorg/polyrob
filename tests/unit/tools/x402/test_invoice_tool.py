"""Money loop — X402InvoiceTool registration gating + action behavior."""
import asyncio

import pytest

from tools.x402 import register_x402_invoice_tool, x402_invoicing_enabled
from tools.x402.invoice_tool import (
    AccountingParams, InvoiceListParams, InvoiceParams, X402InvoiceTool,
)


class _Ctx:
    user_id = "rob"
    session_id = "sess_1"


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("X402_INVOICE_ENABLED", raising=False)
    assert x402_invoicing_enabled() is False
    assert register_x402_invoice_tool() is False


def test_registration_when_enabled(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    assert register_x402_invoice_tool() is True
    from tools.descriptors import TOOL_DESCRIPTORS
    assert "x402_invoice" in TOOL_DESCRIPTORS
    assert TOOL_DESCRIPTORS["x402_invoice"].is_optional


def test_leaf_delegation_blocked():
    from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS
    assert "x402_invoice" in DELEGATE_BLOCKED_TOOLS


def test_x402_request_in_recommended_approval_set():
    from tools.controller.approval import DEFAULT_APPROVAL_REQUIRED_TOOLS
    assert "x402_request" in DEFAULT_APPROVAL_REQUIRED_TOOLS


def test_x402_request_action_formats_instructions(monkeypatch):
    async def fake_create(**kw):
        assert kw["user_id"] == "rob" and kw["session_id"] == "sess_1"
        return {"request_id": "inv_abc", "amount_usd": 5.0, "asset": "usdc",
                "chain": "base", "recipient": "0xT", "purpose": kw["purpose"],
                "expires_at_epoch": 123, "status": "pending"}

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)
    tool = X402InvoiceTool()
    res = asyncio.run(tool.x402_request(
        InvoiceParams(amount_usd=5.0, purpose="research report"),
        execution_context=_Ctx()))
    assert res.error is None
    assert "inv_abc" in res.extracted_content
    assert "$5.00" in res.extracted_content


def test_x402_request_refusal_is_agent_readable(monkeypatch):
    async def fake_create(**kw):
        raise ValueError("daily invoicing cap reached")

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)
    res = asyncio.run(X402InvoiceTool().x402_request(
        InvoiceParams(amount_usd=5.0, purpose="whatever"),
        execution_context=_Ctx()))
    assert res.error and "daily invoicing cap" in res.error


def test_accounting_action(monkeypatch):
    async def fake_build(user_id, *, days, **kw):
        assert user_id == "rob" and days == 7
        return {"user_id": "rob", "window_days": 7, "llm_api_cost_usd": 0.1,
                "credits_spent": 1.0, "llm_calls": 3, "wallet_spend_usd": 0.0,
                "wallet_payments": 0, "earned_usd": 5.0, "settled_payments": 1,
                "pending_invoices_usd": 0.0, "pending_invoices": 0,
                "total_spend_usd": 0.1, "net_usd": 4.9}

    import modules.credits.unified_ledger as ul
    monkeypatch.setattr(ul, "build_ledger", fake_build)
    res = asyncio.run(X402InvoiceTool().accounting(
        AccountingParams(days=7), execution_context=_Ctx()))
    assert res.error is None
    assert "earned" in res.extracted_content and "net" in res.extracted_content


def test_invoices_action_empty(monkeypatch):
    async def fake_list(**kw):
        return []

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "list_payment_requests", fake_list)
    res = asyncio.run(X402InvoiceTool().x402_invoices(
        InvoiceListParams(), execution_context=_Ctx()))
    assert "No payment requests" in res.extracted_content


class _AnonCtx:
    user_id = ""
    session_id = "s"


def test_actions_refuse_anonymous_context():
    tool = X402InvoiceTool()
    for coro in (
        tool.x402_request(InvoiceParams(amount_usd=1.0, purpose="whatever"),
                          execution_context=_AnonCtx()),
        tool.x402_invoices(InvoiceListParams(), execution_context=_AnonCtx()),
        tool.accounting(AccountingParams(), execution_context=_AnonCtx()),
    ):
        res = asyncio.run(coro)
        assert res.error and "authenticated tenant" in res.error
