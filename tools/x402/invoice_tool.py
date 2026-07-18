"""X402InvoiceTool — the agent invoices and accounts for itself.

Three actions, all riding existing seams:

- ``x402_request`` — create a payment request (invoice) from inside a session:
  amount (ceiling-bounded), purpose, an optional free-form ``payer_contact``
  (shown on the invoice), expiry. Outward-facing
  money: default-OFF flag, per-day cap, first-class ``payment_requested`` event,
  and listed in the recommended approval set (operators add ``x402_request`` to
  ``APPROVAL_REQUIRED_TOOLS``; at compute posture ≥2 it is auto-gated). When
  ``INVOICE_CARD_ENABLED`` resolves true, a branded PNG invoice card
  (``modules/pfp/cards.py``) is also rendered into the session workspace and its
  path appended to the result — a presentation nicety over an already-created
  invoice, fail-open (Task 6, Phase 1).
- ``x402_invoices`` — list this tenant's invoices (read-only).
- ``accounting`` — the unified ledger view: treasury (income / spend / net —
  the agent's own money) and runtime cost (the owner's API bill), never
  combined, evidence from usage_records + wallet_spend events + x402
  receipts. Read-only.

Server-tier stores (``modules.x402``/``modules.credits``) are imported lazily at
action time so the core agent import graph stays server-free (C3 boundary).
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool

logger = logging.getLogger(__name__)


class InvoiceParams(BaseModel):
    amount_usd: float = Field(..., gt=0, description="Invoice amount in USD (bounded by X402_INVOICE_MAX_USD)")
    purpose: str = Field(..., min_length=3, description="What the payer is paying for (shown to them and audited)")
    payer_contact: Optional[str] = Field(
        None, description="Free-form contact info for the payer (name/email/handle), "
                          "shown on the invoice.")
    payer_hint: Optional[str] = Field(
        None, description="Deprecated alias for payer_contact (kept for back-compat; "
                          "payer_contact takes precedence when both are given).")
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


def _resolve_workspace_dir(execution_context) -> Optional[str]:
    """Session workspace dir for the invoice card render — mirrors the
    established ``execution_context.workspace_dir`` (when the caller already
    resolved one, e.g. ``tools/filesystem.py``) -> ``pm().get_workspace_dir(...)``
    (e.g. ``tools/code_exec/tool.py``) fallback pattern used elsewhere. Never
    raises; returns ``None`` when neither is resolvable."""
    workspace_dir = getattr(execution_context, "workspace_dir", None)
    if workspace_dir:
        return workspace_dir
    try:
        session_id = getattr(execution_context, "session_id", None)
        if not session_id:
            return None
        user_id = getattr(execution_context, "user_id", None)
        from agents.task.path import pm
        return str(pm().get_workspace_dir(session_id, user_id))
    except Exception:
        return None


def _maybe_render_invoice_card(invoice: Dict[str, Any], execution_context) -> Optional[str]:
    """Best-effort branded PNG invoice card (Task 6). Fail-open BY DESIGN: any
    error here (missing flag, unresolvable workspace, render failure) logs at
    most one WARN and returns None — the invoice already succeeded by the time
    this runs, and a card is a presentation nicety, never a reason to fail an
    already-successful x402_request."""
    try:
        from core.config_policy import invoice_card_enabled
        if not invoice_card_enabled():
            return None
        workspace_dir = _resolve_workspace_dir(execution_context)
        if not workspace_dir:
            return None
        from modules.pfp.cards import render_invoice_card
        from modules.x402.artifact import build_payment_artifact
        out_dir = Path(workspace_dir) / "invoices"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"invoice_{invoice['request_id']}.png"
        artifact = build_payment_artifact(invoice)
        rendered = render_invoice_card(invoice, artifact, out_path)
        return str(rendered)
    except Exception as e:
        logger.warning("x402_request: invoice card render failed (%s) — "
                       "returning the text-only result", e)
        return None


class X402InvoiceTool(BaseTool):
    def __init__(self, name: str = "x402_invoice", config=None, container=None):
        import types
        super().__init__(name=name,
                         config=config if config is not None else types.SimpleNamespace(),
                         container=container)

    def _ar(self, *, content: str = None, error: str = None, metadata: Optional[Dict[str, Any]] = None):
        from tools.controller.types import ActionResult
        if error is not None:
            return ActionResult(error=error)
        return ActionResult(extracted_content=content, metadata=metadata)

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
                payer_contact=params.payer_contact or params.payer_hint,
                expiry_hours=params.expiry_hours,
                correspondent_ref=correspondent_ref,
            )
        except ValueError as e:
            return self._ar(error=f"x402_request refused: {e}")
        except Exception as e:
            logger.error("x402_request failed: %s", e, exc_info=True)
            return self._ar(error=f"x402_request failed: {e}")
        # Task 11 C1 fix: full-precision amount when sub-cent jitter is present
        # (`_dedupe_amount_for_treasury`) — a payer paying the DISPLAYED 2dp
        # amount must never exact-match a DIFFERENT, older same-2dp invoice
        # on-chain (cross-tenant misdirected settlement).
        from modules.x402.artifact import format_invoice_amount
        amount_text = format_invoice_amount(inv['amount_usd'])
        content = (
            f"Payment request created.\n"
            f"  request_id: {inv['request_id']}\n"
            f"  amount: ${amount_text} {inv['asset'].upper()} on {inv['chain']}\n"
            f"  pay to: {inv['recipient']}\n"
            f"  purpose: {inv['purpose']}\n"
            f"  expires: epoch {inv['expires_at_epoch']}\n"
            "Share these instructions with the payer (e.g. via the message tool). "
            "You will be woken in this session when it settles."
        )
        card_path = _maybe_render_invoice_card(inv, execution_context)
        if card_path:
            content += f"\ninvoice card: {card_path}"
        # Structured metadata (Task 9 / G-2): PAYMENT_APPROVAL_MODE=auto's
        # post-execution owner-notify hook reads this instead of parsing `content`.
        return self._ar(content=content, metadata={
            "request_id": inv["request_id"], "amount_usd": inv["amount_usd"],
            "purpose": inv["purpose"],
        })

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
        # M7: full-precision amount via the canonical helper — a jittered
        # $7.5001 invoice must NEVER render as "$7.50" here (the last
        # agent-visible surface that could recreate the C1-class wrong-invoice
        # settle: an agent relaying the 2dp amount makes a payer pay $7.50,
        # which the oldest-first on-chain matcher settles against a DIFFERENT,
        # older $7.50 invoice — a cross-tenant misdirected settlement).
        from modules.x402.artifact import format_invoice_amount
        lines = ["Payment requests (newest first):"]
        for r in rows:
            line = (
                f"  {r['request_id']}: ${format_invoice_amount(r['amount_usd'])} "
                f"[{r['status']}] — {r.get('purpose') or '(no purpose)'} "
                f"(created {r.get('created_at')})")
            if r.get("payer_contact"):
                line += f" — billed to: {r['payer_contact']}"
            lines.append(line)
        return self._ar(content="\n".join(lines))

    @BaseTool.action(
        "Your financial ledger: treasury (income / spend / net / pending — "
        "your own money) and runtime cost (what the owner pays to run you), "
        "over a trailing window. The two are never combined. Read-only, "
        "evidence-backed.",
        param_model=AccountingParams)
    async def accounting(self, params: AccountingParams, execution_context=None):
        user_id, _ = _ctx_ids(execution_context)
        if not user_id:
            return self._ar(error=f"accounting refused: {_ANON_REFUSED}")
        try:
            from modules.credits.unified_ledger import build_ledger, format_ledger
            ledger = await build_ledger(user_id, days=params.days, include_balances=True)
            return self._ar(content=format_ledger(ledger))
        except Exception as e:
            logger.error("accounting failed: %s", e, exc_info=True)
            return self._ar(error=f"accounting failed: {e}")
