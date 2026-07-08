"""Settlement watcher: the agent experiences
"I invoiced → I got paid" as one continuous piece of work.

A small ticker (same shape as the cron/goal tickers, wired through
``core/autonomy_runtime.start_autonomy``) that each tick:

1. expires pending invoices past their deadline (``payment_expired`` events);
2. finds settled-but-unnotified agent invoices and re-enters each one's
   originating session via the existing self-wake rail
   (``TaskAgent.deliver_self_wake`` — kind is always ``self_wake``; the payment
   context rides in ``metadata`` per the UP-12/W1 contract), then emits the
   first-class ``payment_settled`` event and marks the row notified.

The wake is best-effort (SELF_WAKE_ENABLED off / non-resident session / budget
exhausted → dropped): the settled row + ``payment_settled`` event remain the
durable record either way, so the row is marked notified exactly once and the
agent/owner can always reconcile from the ledger. Every step is fail-open —
a watcher error never breaks the autonomy runtime.
"""
import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SettlementWatcher:
    """Poll pending/settled agent invoices; deliver settlement wakes."""

    def __init__(self, task_agent: Any, *, db=None, interval_seconds: int = 60):
        self.task_agent = task_agent
        self._db = db
        self.interval_seconds = interval_seconds

    async def tick_once(self) -> dict:
        from modules.x402 import invoicing

        expired = []
        settled = []
        try:
            expired = await invoicing.expire_stale_requests(db=self._db)
        except Exception:
            logger.warning("settlement watcher: expiry sweep failed", exc_info=True)
        try:
            settled = await invoicing.settled_unnotified_invoices(db=self._db)
        except Exception:
            logger.warning("settlement watcher: settled poll failed", exc_info=True)

        notified = 0
        for inv in settled:
            try:
                # Claim-then-notify: the atomic claim makes concurrent watcher
                # processes safe — exactly one delivers the wake + event.
                if not await invoicing.claim_wake(inv["request_id"], db=self._db):
                    continue
                await self._notify(inv)
                notified += 1
            except Exception:
                logger.warning("settlement watcher: notify failed for %s "
                               "(claim consumed — ledger row is the record)",
                               inv.get("request_id"), exc_info=True)
        return {"expired": len(expired), "settled_notified": notified}

    async def _notify(self, inv: dict) -> None:
        from modules.x402.invoicing import _emit

        _emit("payment_settled", user_id=inv.get("user_id") or "",
              session_id=inv.get("session_id") or "", attrs={
                  "request_id": inv["request_id"],
                  "amount_usd": inv.get("amount_usd"),
                  "transaction_hash": inv.get("transaction_hash"),
              })
        session_id = inv.get("session_id") or ""
        if not session_id:
            return
        text = (
            f"Payment request {inv['request_id']} has SETTLED: "
            f"${float(inv.get('amount_usd') or 0):.2f} received"
            + (f" (tx {inv['transaction_hash']})" if inv.get("transaction_hash") else "")
            + f". Purpose: {inv.get('purpose') or '(unspecified)'}. "
            "Continue the work this payment was for, or acknowledge and close it out."
        )
        # A correspondent-linked invoice (a third party the agent contacted) is
        # delivered as DATA on the correspondent rail — never the owner "obey"
        # queue — so a payer can settle without gaining steering rights.
        cref = inv.get("correspondent_ref")
        if cref and self._correspondent_active(cref):
            deliver_corr = getattr(self.task_agent, "deliver_correspondent_data", None)
            if deliver_corr is not None:
                src = f"{cref.get('surface', '')}:{cref.get('address', '')}"
                delivered = await deliver_corr(
                    session_id, src, text,
                    {"kind_hint": "payment_settled", "request_id": inv["request_id"]})
                if not delivered:
                    logger.info("settlement correspondent-data for %s dropped — "
                                "ledger row is the record", inv["request_id"])
                return

        deliver = getattr(self.task_agent, "deliver_self_wake", None)
        if deliver is None:
            return
        delivered = await deliver(
            session_id, inv.get("user_id") or "", text,
            metadata={"kind_hint": "payment_settled",
                      "request_id": inv["request_id"]},
        )
        if not delivered:
            logger.info("settlement wake for %s dropped (self-wake disabled/"
                        "budget/non-resident) — ledger row is the record",
                        inv["request_id"])

    def _correspondent_active(self, cref: dict) -> bool:
        """True only when correspondent access is enabled AND the payer's registry
        row is ACTIVE — else fall back to an owner self-wake. Fail-open to False."""
        try:
            if not isinstance(cref, dict) or not cref.get("surface") or not cref.get("address"):
                return False
            from agents.task.surface_config import SurfaceConfig
            if not SurfaceConfig.correspondent_access_enabled():
                return False
            container = getattr(self.task_agent, "container", None)
            reg = container.get_service("correspondent_registry") if container else None
            if reg is None:
                return False
            row = reg.resolve(surface=cref.get("surface"), address=cref.get("address"),
                              thread_id=cref.get("thread_id") or None)
            return bool(row and row.get("state") == "active")
        except Exception:
            return False

    async def run_forever(self, stop_event: Optional[asyncio.Event] = None) -> None:
        from core.tickers import IntervalTicker

        await IntervalTicker(self.tick_once, self.interval_seconds).run_forever(
            stop_event=stop_event)


def build_settlement_watcher(task_agent: Any, *, interval_seconds: Optional[int] = None) -> SettlementWatcher:
    import os
    if interval_seconds is None:
        try:
            interval_seconds = int(os.getenv("X402_SETTLEMENT_WATCH_INTERVAL_SEC", "60"))
        except ValueError:
            interval_seconds = 60
    return SettlementWatcher(task_agent, interval_seconds=interval_seconds)
