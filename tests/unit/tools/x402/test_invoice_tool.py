"""Money loop — X402InvoiceTool registration gating + action behavior."""
import asyncio
from pathlib import Path

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


def test_x402_request_result_carries_structured_metadata(monkeypatch):
    """Task 9 / G-2: PAYMENT_APPROVAL_MODE=auto's post-execution owner-notify hook
    reads request_id/amount_usd/purpose off ActionResult.metadata rather than
    parsing the human-readable content string."""
    async def fake_create(**kw):
        return {"request_id": "inv_meta1", "amount_usd": 12.5, "asset": "usdc",
                "chain": "base", "recipient": "0xT", "purpose": kw["purpose"],
                "expires_at_epoch": 123, "status": "pending"}

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)
    res = asyncio.run(X402InvoiceTool().x402_request(
        InvoiceParams(amount_usd=12.5, purpose="hosting"),
        execution_context=_Ctx()))
    assert res.error is None
    assert res.metadata == {"request_id": "inv_meta1", "amount_usd": 12.5, "purpose": "hosting"}


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
    seen = {}

    async def fake_build(user_id, *, days, include_balances=False, **kw):
        assert user_id == "rob" and days == 7
        seen["include_balances"] = include_balances
        return {"user_id": "rob", "window_days": 7, "llm_api_cost_usd": 0.1,
                "credits_spent": 1.0, "llm_calls": 3, "wallet_spend_usd": 0.0,
                "wallet_payments": 0, "settled_payments": 1,
                "pending_invoices_usd": 0.0, "pending_invoices": 0,
                "treasury": {"income_usd": 5.0, "spend_usd": 0.0,
                             "pending_usd": 0.0, "pending_count": 0,
                             "balance_usd": None, "net_usd": 5.0,
                             "available": True},
                "runtime": {"spend_window_usd": 0.1, "spend_total_usd": 0.1,
                            "calls_window": 3, "calls_total": 3,
                            "provider_balance_usd": None, "available": True}}

    import modules.credits.unified_ledger as ul
    monkeypatch.setattr(ul, "build_ledger", fake_build)
    res = asyncio.run(X402InvoiceTool().accounting(
        AccountingParams(days=7), execution_context=_Ctx()))
    assert res.error is None
    assert "income" in res.extracted_content and "net" in res.extracted_content
    assert "Runtime cost" in res.extracted_content   # both statements render
    assert seen["include_balances"] is True          # display surface requests balances


def test_invoices_action_empty(monkeypatch):
    async def fake_list(**kw):
        return []

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "list_payment_requests", fake_list)
    res = asyncio.run(X402InvoiceTool().x402_invoices(
        InvoiceListParams(), execution_context=_Ctx()))
    assert "No payment requests" in res.extracted_content


class _CtxWithWorkspace:
    """A test double carrying `workspace_dir` — the invoice-card render seam
    (Task 6) reads it via `getattr(execution_context, "workspace_dir", None)`."""

    def __init__(self, workspace_dir):
        self.user_id = "rob"
        self.session_id = "sess_1"
        self.workspace_dir = str(workspace_dir)


def _fake_create_kwargs(**kw):
    return {"request_id": "inv_card1", "amount_usd": 5.0, "asset": "usdc",
            "chain": "base", "recipient": "0xT", "purpose": kw["purpose"],
            "expires_at_epoch": 123, "status": "pending"}


def test_invoice_card_flag_off_result_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("INVOICE_CARD_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)

    async def fake_create(**kw):
        return _fake_create_kwargs(**kw)

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)
    tool = X402InvoiceTool()
    res = asyncio.run(tool.x402_request(
        InvoiceParams(amount_usd=5.0, purpose="widget"),
        execution_context=_CtxWithWorkspace(tmp_path)))
    assert res.error is None
    assert "invoice card:" not in res.extracted_content
    # byte-identical to the pre-Task-6 result shape
    assert res.extracted_content == (
        "Payment request created.\n"
        "  request_id: inv_card1\n"
        "  amount: $5.00 USDC on base\n"
        "  pay to: 0xT\n"
        "  purpose: widget\n"
        "  expires: epoch 123\n"
        "Share these instructions with the payer (e.g. via the message tool). "
        "You will be woken in this session when it settles."
    )


def test_invoice_card_flag_on_renders_and_appends_path(monkeypatch, tmp_path):
    monkeypatch.setenv("INVOICE_CARD_ENABLED", "true")

    async def fake_create(**kw):
        return _fake_create_kwargs(**kw)

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)
    tool = X402InvoiceTool()
    res = asyncio.run(tool.x402_request(
        InvoiceParams(amount_usd=5.0, purpose="widget"),
        execution_context=_CtxWithWorkspace(tmp_path)))
    assert res.error is None
    assert "invoice card:" in res.extracted_content
    card_line = [l for l in res.extracted_content.splitlines() if l.startswith("invoice card:")][0]
    card_path = Path(card_line.split("invoice card:", 1)[1].strip())
    assert card_path.is_file()
    assert card_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert str(tmp_path) in str(card_path)


def test_invoice_card_render_failure_is_fail_open(monkeypatch, tmp_path):
    monkeypatch.setenv("INVOICE_CARD_ENABLED", "true")

    async def fake_create(**kw):
        return _fake_create_kwargs(**kw)

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)

    import modules.pfp.cards as cards

    def boom(*a, **kw):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(cards, "render_invoice_card", boom)
    tool = X402InvoiceTool()
    res = asyncio.run(tool.x402_request(
        InvoiceParams(amount_usd=5.0, purpose="widget"),
        execution_context=_CtxWithWorkspace(tmp_path)))
    assert res.error is None
    assert "invoice card:" not in res.extracted_content
    assert "inv_card1" in res.extracted_content


def test_invoice_card_no_workspace_is_fail_open(monkeypatch):
    # execution_context with no workspace_dir and no session_id -> can't resolve
    # a place to write the card; must degrade to the text-only result, never raise.
    monkeypatch.setenv("INVOICE_CARD_ENABLED", "true")

    async def fake_create(**kw):
        return _fake_create_kwargs(**kw)

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)

    class _NoWorkspaceCtx:
        user_id = "rob"
        session_id = ""

    tool = X402InvoiceTool()
    res = asyncio.run(tool.x402_request(
        InvoiceParams(amount_usd=5.0, purpose="widget"),
        execution_context=_NoWorkspaceCtx()))
    assert res.error is None
    assert "invoice card:" not in res.extracted_content


# --- Task 8: free-form payer_contact (payer_hint promoted to first-class) --

def test_x402_request_passes_payer_contact(monkeypatch):
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return {"request_id": "inv_pc", "amount_usd": 5.0, "asset": "usdc",
                "chain": "base", "recipient": "0xT", "purpose": kw["purpose"],
                "expires_at_epoch": 123, "status": "pending",
                "payer_contact": kw.get("payer_contact")}

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)
    res = asyncio.run(X402InvoiceTool().x402_request(
        InvoiceParams(amount_usd=5.0, purpose="research report",
                     payer_contact="Alice <a@x.com>"),
        execution_context=_Ctx()))
    assert res.error is None
    assert captured["payer_contact"] == "Alice <a@x.com>"


def test_x402_request_payer_hint_alias_maps_to_payer_contact(monkeypatch):
    """The deprecated payer_hint param still works — accepted as an alias."""
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return {"request_id": "inv_ph", "amount_usd": 5.0, "asset": "usdc",
                "chain": "base", "recipient": "0xT", "purpose": kw["purpose"],
                "expires_at_epoch": 123, "status": "pending",
                "payer_contact": kw.get("payer_contact")}

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)
    res = asyncio.run(X402InvoiceTool().x402_request(
        InvoiceParams(amount_usd=5.0, purpose="research report",
                     payer_hint="Bob <b@x.com>"),
        execution_context=_Ctx()))
    assert res.error is None
    assert captured["payer_contact"] == "Bob <b@x.com>"


def test_x402_request_payer_contact_takes_precedence_over_payer_hint(monkeypatch):
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return {"request_id": "inv_pref", "amount_usd": 5.0, "asset": "usdc",
                "chain": "base", "recipient": "0xT", "purpose": kw["purpose"],
                "expires_at_epoch": 123, "status": "pending",
                "payer_contact": kw.get("payer_contact")}

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)
    asyncio.run(X402InvoiceTool().x402_request(
        InvoiceParams(amount_usd=5.0, purpose="research report",
                     payer_contact="Alice <a@x.com>", payer_hint="Bob <b@x.com>"),
        execution_context=_Ctx()))
    assert captured["payer_contact"] == "Alice <a@x.com>"


def test_x402_request_neither_payer_field_passes_none(monkeypatch):
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return {"request_id": "inv_none", "amount_usd": 5.0, "asset": "usdc",
                "chain": "base", "recipient": "0xT", "purpose": kw["purpose"],
                "expires_at_epoch": 123, "status": "pending",
                "payer_contact": kw.get("payer_contact")}

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "create_payment_request", fake_create)
    res = asyncio.run(X402InvoiceTool().x402_request(
        InvoiceParams(amount_usd=5.0, purpose="research report"),
        execution_context=_Ctx()))
    assert res.error is None
    assert captured["payer_contact"] is None


def test_x402_invoices_action_shows_billed_to(monkeypatch):
    async def fake_list(**kw):
        return [{"request_id": "inv_1", "amount_usd": 5.0, "status": "pending",
                 "purpose": "widget", "created_at": "2026-01-01",
                 "payer_contact": "Alice <a@x.com>"}]

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "list_payment_requests", fake_list)
    res = asyncio.run(X402InvoiceTool().x402_invoices(
        InvoiceListParams(), execution_context=_Ctx()))
    assert res.error is None
    assert "billed to" in res.extracted_content.lower()
    assert "Alice <a@x.com>" in res.extracted_content


def test_x402_invoices_action_omits_billed_to_when_absent(monkeypatch):
    async def fake_list(**kw):
        return [{"request_id": "inv_1", "amount_usd": 5.0, "status": "pending",
                 "purpose": "widget", "created_at": "2026-01-01",
                 "payer_contact": None}]

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "list_payment_requests", fake_list)
    res = asyncio.run(X402InvoiceTool().x402_invoices(
        InvoiceListParams(), execution_context=_Ctx()))
    assert res.error is None
    assert "billed to" not in res.extracted_content.lower()


def test_m7_invoices_listing_renders_full_precision_jittered_amount(monkeypatch):
    """M7: the listing must render a jittered $7.5001 invoice at full precision,
    NOT re-truncate to $7.50 — a 2dp relay would let a payer pay $7.50 and settle
    a DIFFERENT older same-2dp invoice on-chain (cross-tenant misdirect)."""
    async def fake_list(**kw):
        return [{"request_id": "inv_1", "amount_usd": 7.5001, "status": "pending",
                 "purpose": "widget", "created_at": "2026-01-01",
                 "payer_contact": None}]

    import modules.x402.invoicing as inv
    monkeypatch.setattr(inv, "list_payment_requests", fake_list)
    res = asyncio.run(X402InvoiceTool().x402_invoices(
        InvoiceListParams(), execution_context=_Ctx()))
    assert res.error is None
    assert "$7.5001" in res.extracted_content
    assert "$7.50 " not in res.extracted_content  # never re-truncated to 2dp


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
