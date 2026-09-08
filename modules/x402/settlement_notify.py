"""The notification rails: ONE session-side delivery helper (correspondent DATA else owner self-wake) and ONE durable owner notice (`_push_owner_notice`), used by settlement, expiry, subscriptions and anomalies (B10, 2026-08-29).

Split out of ``modules/x402/settlement_watcher.py`` (S6, 2026-08-29). A mixin over
``SettlementWatcher`` — it relies on the host for ``task_agent``, ``_db``, the injection
seams (``_rpc_call``/``_usdc_addr``/``_goal_board_override``/``_reputation_manager_override``)
and the module ``logger``; behaviour is byte-identical to the pre-split class.
"""
import logging
from typing import Any, Optional
import time

logger = logging.getLogger("modules.x402.settlement_watcher")

class SettlementNotifyMixin:
    async def _deliver_session_notice(self, inv: dict, text: str, kind_hint: str) -> bool:
        """Session-side delivery shared by settlement and expiry (B10, 2026-08-29).

        A correspondent-linked invoice (a third party the agent contacted) is
        delivered as DATA on the correspondent rail — never the owner "obey"
        queue — so a payer can settle without gaining steering rights; otherwise
        the originating session is re-entered over the self-wake rail. Returns
        True when a correspondent delivery was attempted (the 8004 feedback hook
        keys on that), False otherwise. Never raises.
        """
        session_id = inv.get("session_id") or ""
        request_id = inv.get("request_id")
        if not session_id:
            return False
        cref = inv.get("correspondent_ref")
        if cref and self._correspondent_active(cref):
            deliver_corr = getattr(self.task_agent, "deliver_correspondent_data", None)
            if deliver_corr is not None:
                src = f"{cref.get('surface', '')}:{cref.get('address', '')}"
                delivered = await deliver_corr(
                    session_id, src, text,
                    {"kind_hint": kind_hint, "request_id": request_id})
                if not delivered:
                    logger.info("%s correspondent-data for %s dropped — "
                                "ledger row is the record", kind_hint, request_id)
            return True
        deliver = getattr(self.task_agent, "deliver_self_wake", None)
        if deliver is None:
            return False
        delivered = await deliver(
            session_id, inv.get("user_id") or "", text,
            metadata={"kind_hint": kind_hint, "request_id": request_id},
        )
        if not delivered:
            logger.info("%s wake for %s dropped (self-wake disabled/budget/"
                        "non-resident) — ledger row is the record", kind_hint, request_id)
        return False
    async def _notify(self, inv: dict) -> None:
        """Settlement notification: the first-class ``payment_settled`` event, the
        session-side notice (correspondent DATA or owner self-wake) and — since
        B10 (2026-08-29) — an UNCONDITIONAL owner notification over the durable
        delivery rail, the same one :meth:`_notify_expired` always used. Money
        ARRIVING must reach the owner even when the session cannot be woken; the
        rail dedups/caps, so a resident session's wake and this notice never
        double-page. Emits the event exactly once; the caller marks the row."""
        from modules.x402.invoicing import _emit

        _emit("payment_settled", user_id=inv.get("user_id") or "",
              session_id=inv.get("session_id") or "", attrs={
                  "request_id": inv["request_id"],
                  "amount_usd": inv.get("amount_usd"),
                  "transaction_hash": inv.get("transaction_hash"),
              })
        amount = float(inv.get("amount_usd") or 0)
        text = (
            f"Payment request {inv['request_id']} has SETTLED: "
            f"${amount:.2f} received"
            + (f" (tx {inv['transaction_hash']})" if inv.get("transaction_hash") else "")
            + f". Purpose: {inv.get('purpose') or '(unspecified)'}. "
            "Continue the work this payment was for, or acknowledge and close it out."
        )
        via_correspondent = await self._deliver_session_notice(inv, text, "payment_settled")
        if via_correspondent:
            # Task 15 (Phase 4): the settlement notice's target IS an
            # identifiable payer (an ACTIVE correspondent channel) — exactly
            # the anti-sybil "who paid" signal ERC-8004 payment-backed
            # feedback needs. Fail-open: an 8004 error must NEVER affect the
            # settlement notice above (already delivered by this point).
            try:
                await self._maybe_offer_payment_feedback(
                    inv, inv.get("session_id") or "", inv.get("correspondent_ref"))
            except Exception:
                logger.warning(
                    "settlement watcher: eip8004 payment-feedback hook failed "
                    "for %s (fail-open, settlement unaffected)",
                    inv.get("request_id"), exc_info=True)
        owner_text = (f"Invoice {inv['request_id']} SETTLED: ${amount:.2f} received"
                      + (f" (tx {inv['transaction_hash']})" if inv.get("transaction_hash") else "")
                      + ".")
        await self._push_owner_notice(inv.get("user_id") or "", owner_text,
                                      source="x402_invoice",
                                      session_id=inv.get("session_id") or None)
    async def _notify_expired(self, inv: dict) -> None:
        """Non-payment escalation (G-22): the session-side notice (correspondent
        DATA or an owner self-wake — the SAME rails :meth:`_notify` uses) PLUS an
        unconditional owner notification over the durable delivery rail, so a
        non-payment is never invisible to the owner even when the session can't
        be woken.

        The ``payment_expired`` telemetry event was already emitted by
        :func:`invoicing.expire_stale_requests` at expiry time — this method
        is notification-only and must NOT re-emit it."""
        request_id = inv.get("request_id")
        amount = float(inv.get("amount_usd") or 0)
        purpose = inv.get("purpose") or "(unspecified)"
        text = (
            f"Payment request {request_id} EXPIRED unpaid (${amount:.2f}, "
            f"purpose: {purpose}). The payer did not pay within the window."
        )
        await self._deliver_session_notice(inv, text, "payment_expired")
        await self._push_owner_notice(
            inv.get("user_id") or "",
            f"Invoice {request_id} for ${amount:.2f} expired unpaid.",
            source="x402_invoice", session_id=inv.get("session_id") or None)
    def _correspondent_active(self, cref: dict) -> bool:
        """True only when correspondent access is enabled AND the payer's registry
        row is ACTIVE — else fall back to an owner self-wake. Fail-open to False."""
        try:
            if not isinstance(cref, dict) or not cref.get("surface") or not cref.get("address"):
                return False
            from core.surfaces.config import SurfaceConfig
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
    def _goal_board(self):
        """The durable-ask store the approve-mode renewal flow rides
        (`agents.task.goals.board.GoalBoard`). Lazily built against the SAME
        `goals.db` the owner CLI / dispatcher use unless a test injected one
        via the constructor's `goal_board` seam."""
        if self._goal_board_override is not None:
            return self._goal_board_override
        import os
        from agents.task.goals.board import GoalBoard
        from core.runtime_config import get_data_root
        return GoalBoard(os.path.join(get_data_root(), "goals.db"))
    async def _push_owner_notice(self, user_id: str, text: str,
                                 source: str = "subscriptions",
                                 session_id: Optional[str] = None) -> None:
        """Best-effort owner notification over the ONE durable delivery rail
        (`core/surfaces/user_delivery.py`) — settlement, expiry, subscription
        and anomaly notices all ride it. The rail is itself fail-open with a
        durable owner_notice fallback, so this never blocks the tick. Never raises."""
        if not user_id or not text:
            return
        try:
            import core.surfaces.user_delivery as _ud
            container = getattr(self.task_agent, "container", None)
            await _ud.deliver_user_message(container, user_id, text, source=source,
                                           session_id=session_id)
        except Exception:
            logger.debug("settlement watcher: owner notice failed "
                        "(fail-open)", exc_info=True)
    async def _resolve_correspondent_session(self, sub: dict) -> Optional[str]:
        """The session_id an ACTIVE correspondent binding for this
        subscription's (surface, address) resolves to — or None when
        correspondent access is off, unconfigured, or there is no active
        binding yet. Mirrors `_correspondent_active` but returns the routable
        session_id instead of a bool (the caller needs it to actually
        deliver). Fail-open to None."""
        try:
            from core.surfaces.config import SurfaceConfig
            if not SurfaceConfig.correspondent_access_enabled():
                return None
            container = getattr(self.task_agent, "container", None)
            reg = container.get_service("correspondent_registry") if container else None
            if reg is None:
                return None
            row = reg.resolve(surface=sub.get("correspondent_surface"),
                              address=sub.get("correspondent_address"))
            if row and row.get("state") == "active":
                return row.get("session_id") or None
        except Exception:
            logger.debug("settlement watcher: correspondent session resolve "
                        "failed for subscription %s", sub.get("id"), exc_info=True)
        return None
