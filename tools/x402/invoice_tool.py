"""X402InvoiceTool — the agent invoices and accounts for itself.

Three actions, all riding existing seams:

- ``x402_request`` — create a payment request (invoice) from inside a session:
  amount (ceiling-bounded), purpose, optional payer hint, expiry. Outward-facing
  money: default-OFF flag, per-day cap, first-class ``payment_requested`` event,
  and listed in the recommended approval set (operators add ``x402_request`` to
  ``APPROVAL_REQUIRED_TOOLS``; at compute posture ≥2 it is auto-gated).
- ``x402_invoices`` — list this tenant's invoices (read-only).
- ``accounting`` — the unified ledger view (earned / spent / pending, evidence
  from usage_records + wallet_spend events + x402 receipts). Read-only.

Server-tier stores (``modules.x402``/``modules.credits``) are imported lazily at
action time so the core agent import graph stays server-free (C3 boundary).
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import logging
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool

logger = logging.getLogger(__name__)


class InvoiceParams(BaseModel):
    amount_usd: float = Field(..., gt=0, description="Invoice amount in USD (bounded by X402_INVOICE_MAX_USD)")
    purpose: str = Field(..., min_length=3, description="What the payer is paying for (shown to them and audited)")
    payer_hint: Optional[str] = Field(None, description="Optional payer identity/contact hint for your own records")
    expiry_hours: float = Field(72.0, gt=0, le=24 * 30, description="Hours until the request expires (default 72)")
    payer_surface: Optional[str] = Field(
        None, description="If invoicing a correspondent you already contacted, their surface "
                          "(e.g. 'email'/'telegram'); settlement is delivered to you as DATA, not a command")
    payer_address: Optional[str] = Field(
        None, description="The correspondent payer's address/handle on that surface (pairs with payer_surface)")


class InvoiceListParams(BaseModel):
    status: Optional[str] = Field(None, description="Filter: pending|completed|expired (default all)")


class AccountingParams(BaseModel):
    days: int = Field(7, ge=1, le=365, description="Trailing window in days (default 7)")


def _ctx_ids(execution_context) -> tuple:
    user_id = getattr(execution_context, "user_id", "") or ""
    session_id = getattr(execution_context, "session_id", "") or ""
    return user_id, session_id


_ANON_REFUSED = ("this action requires an authenticated tenant — the execution "
                 "context has no user_id (anonymous financial state is refused)")


class X402InvoiceTool(BaseTool):
    def __init__(self, name: str = "x402_invoice", config=None, container=None):
        import types
        super().__init__(name=name,
                         config=config if config is not None else types.SimpleNamespace(),
                         container=container)

    def _ar(self, *, content: str = None, error: str = None):
        from tools.controller.types import ActionResult
        if error is not None:
            return ActionResult(error=error)
        return ActionResult(extracted_content=content)

    @BaseTool.action(
        "Create an x402 payment request (invoice): amount, purpose, expiry. "
        "Returns payment instructions the payer needs (recipient/chain/amount/id).",
        param_model=InvoiceParams)
    async def x402_request(self, params: InvoiceParams, execution_context=None):
        user_id, session_id = _ctx_ids(execution_context)
        if not user_id:
            return self._ar(error=f"x402_request refused: {_ANON_REFUSED}")
        try:
            from modules.x402.invoicing import create_payment_request
            correspondent_ref = None
            if params.payer_surface and params.payer_address:
                correspondent_ref = {"surface": params.payer_surface,
                                     "address": params.payer_address, "thread_id": ""}
            inv = await create_payment_request(
                user_id=user_id, session_id=session_id,
                amount_usd=params.amount_usd, purpose=params.purpose,
                payer_hint=params.payer_hint, expiry_hours=params.expiry_hours,
                correspondent_ref=correspondent_ref,
            )
        except ValueError as e:
            return self._ar(error=f"x402_request refused: {e}")
        except Exception as e:
            logger.error("x402_request failed: %s", e, exc_info=True)
            return self._ar(error=f"x402_request failed: {e}")
        return self._ar(content=(
            f"Payment request created.\n"
            f"  request_id: {inv['request_id']}\n"
            f"  amount: ${inv['amount_usd']:.2f} {inv['asset'].upper()} on {inv['chain']}\n"
            f"  pay to: {inv['recipient']}\n"
            f"  purpose: {inv['purpose']}\n"
            f"  expires: epoch {inv['expires_at_epoch']}\n"
            "Share these instructions with the payer (e.g. via the message tool). "
            "You will be woken in this session when it settles."
        ))

    @BaseTool.action("List your x402 payment requests (invoices) and their status",
                     param_model=InvoiceListParams)
    async def x402_invoices(self, params: InvoiceListParams, execution_context=None):
        user_id, _ = _ctx_ids(execution_context)
        if not user_id:
            return self._ar(error=f"x402_invoices refused: {_ANON_REFUSED}")
        try:
            from modules.x402.invoicing import list_payment_requests
            rows = await list_payment_requests(user_id=user_id, status=params.status)
        except Exception as e:
            return self._ar(error=f"x402_invoices failed: {e}")
        if not rows:
            return self._ar(content="No payment requests found.")
        lines = ["Payment requests (newest first):"]
        for r in rows:
            lines.append(
                f"  {r['request_id']}: ${float(r['amount_usd'] or 0):.2f} "
                f"[{r['status']}] — {r.get('purpose') or '(no purpose)'} "
                f"(created {r.get('created_at')})")
        return self._ar(content="\n".join(lines))

    @BaseTool.action(
        "Your unified financial ledger: earned / pending / spent (LLM + wallet) "
        "over a trailing window. Read-only, evidence-backed.",
        param_model=AccountingParams)
    async def accounting(self, params: AccountingParams, execution_context=None):
        user_id, _ = _ctx_ids(execution_context)
        if not user_id:
            return self._ar(error=f"accounting refused: {_ANON_REFUSED}")
        try:
            from modules.credits.unified_ledger import build_ledger, format_ledger
            ledger = await build_ledger(user_id, days=params.days)
            return self._ar(content=format_ledger(ledger))
        except Exception as e:
            logger.error("accounting failed: %s", e, exc_info=True)
            return self._ar(error=f"accounting failed: {e}")
