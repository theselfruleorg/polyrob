"""Watchtower subscriptions (`SUBSCRIPTIONS_ENABLED`): renewal invoices (auto / owner-approved), lapse ladder active -> grace -> suspended, apply-failure anomaly notice.

Split out of ``modules/x402/settlement_watcher.py`` (S6, 2026-08-29). A mixin over
``SettlementWatcher`` — it relies on the host for ``task_agent``, ``_db``, the injection
seams (``_rpc_call``/``_usdc_addr``/``_goal_board_override``/``_reputation_manager_override``)
and the module ``logger``; behaviour is byte-identical to the pre-split class.
"""
import logging
from typing import Any, Optional
import time

logger = logging.getLogger("modules.x402.settlement_watcher")

class SettlementSubscriptionsMixin:
    async def _process_subscriptions(self) -> dict:
        """Task 14 tick step: create renewal invoices ahead of `paid_through`
        (respecting PAYMENT_APPROVAL_MODE), then move lapsed subscriptions
        through active -> grace -> suspended. Gated `SUBSCRIPTIONS_ENABLED` —
        OFF returns immediately without touching the subscriptions table at
        all (byte-identical tick)."""
        from modules.x402 import subscriptions as subs
        if not subs.subscriptions_enabled():
            return {"subscription_renewals_invoiced": 0, "subscription_grace": 0,
                    "subscription_suspended": 0}

        now = time.time()
        renewed = 0
        due = await subs.subscriptions_needing_renewal(now=now, db=self._db)
        for sub in due:
            try:
                if await self._request_or_create_renewal(sub):
                    renewed += 1
            except Exception:
                logger.warning("settlement watcher: renewal step failed for "
                               "subscription %s", sub.get("id"), exc_info=True)

        graced = await subs.subscriptions_to_grace(now=now, db=self._db)

        suspended = await subs.subscriptions_to_suspend(now=now, db=self._db)
        for sub in suspended:
            try:
                await self._notify_suspended(sub)
            except Exception:
                logger.warning("settlement watcher: suspend notice failed for "
                               "subscription %s", sub.get("id"), exc_info=True)

        return {"subscription_renewals_invoiced": renewed,
                "subscription_grace": len(graced),
                "subscription_suspended": len(suspended)}
    async def _request_or_create_renewal(self, sub: dict) -> bool:
        """Decide + act on ONE due subscription's renewal, respecting
        PAYMENT_APPROVAL_MODE (Task 14 — "renewals are the auto-mode poster
        child, but respect the mode"):

        - a prior tick's owner-APPROVED renewal ask, not yet consumed, is
          consumed now and the invoice is created (works in EITHER mode —
          an owner can always pre-approve);
        - ``auto``: create the invoice immediately, no queueing;
        - ``approve``: an OPEN ask already awaiting a decision -> do nothing
          this tick; a RECENTLY REJECTED ask -> back off for a day (don't
          spam a fresh ask every tick); otherwise queue a new durable owner
          ask (the settlement-watcher tick never blocks waiting on it — the
          decision is resolved on a LATER tick, unlike the interactive
          `OwnerQueueApprover` an agent tool call would use).

        Returns True iff an invoice was actually created this call.
        """
        # H5 (renewals leg): the owner kill-switch halts ALL autonomous minting —
        # including auto-mode subscription renewals (the review's cited renewal
        # gap). Check FIRST, before any state mutation (grant consume / new ask),
        # so a halt SKIPS cleanly WITHOUT burning one-shot state — the renewal
        # simply resumes on the next tick after `polyrob owner resume`. Fail
        # CLOSED: a probe error skips too.
        try:
            from core.config_policy import AutonomyConfig
            _halted = AutonomyConfig.autonomy_halted()
        except Exception:
            _halted = True
        if _halted:
            logger.info(
                "settlement watcher: subscription %s renewal skipped — autonomy "
                "HALTED (owner kill-switch); retries on the next tick after resume",
                sub.get("id"))
            return False
        from core.config_policy import payment_approval_mode, approval_grant_ttl_hours

        board = self._goal_board()
        sub_id = sub["id"]
        tenant = sub["user_id"]
        current_amount = float(sub["amount_usd"])
        ttl_seconds = approval_grant_ttl_hours() * 3600
        now = time.time()
        matching = [
            a for a in board.asks(user_id=tenant)
            if (a.payload or {}).get("ask_kind") == self._RENEWAL_ASK_KIND
            and (a.payload or {}).get("tool_name") == self._RENEWAL_ASK_TOOL
            and (a.payload or {}).get("subscription_id") == sub_id
        ]
        for a in matching:
            payload = a.payload or {}
            if a.status == "fulfilled" and not payload.get("grant_consumed"):
                # M11: a renewal grant carries the SAME TTL as an interactive
                # owner-queue grant (`approval_queue._consume_grant`) AND is bound
                # to the amount the owner approved. A months-old approval, or one
                # for a different amount than the subscription now charges, must
                # NOT silently mint an invoice — skip it and (re-)ask.
                if a.completed_at is None or (now - a.completed_at) > ttl_seconds:
                    continue  # expired grant
                approved_amount = payload.get("amount_usd")
                if approved_amount is not None and \
                        abs(float(approved_amount) - current_amount) > 1e-9:
                    logger.warning(
                        "settlement watcher: renewal grant for subscription %s "
                        "approved $%.4f but the subscription now charges $%.4f — "
                        "refusing to consume the stale-amount grant, re-asking",
                        sub_id, float(approved_amount), current_amount)
                    continue  # amount mismatch — do not consume
                if board.consume_ask_grant(a.id):
                    return await self._create_renewal_invoice(sub) is not None
            elif a.status == "open":
                return False  # still awaiting an owner decision
            elif a.status == "rejected":
                age_days = (time.time() - (a.completed_at or 0)) / 86400
                if age_days < 1.0:
                    return False  # recently declined — don't immediately re-ask

        if payment_approval_mode() == "auto":
            return await self._create_renewal_invoice(sub) is not None

        # approve mode, no live/recent ask found: queue a new one. Reuses the
        # EXISTING tool_approval ask kind, so `polyrob owner pending` /
        # `owner promote tool_approval <id>` already handle it — no new
        # owner-facing surface.
        ask = board.create_ask(
            user_id=tenant,
            what=f"Approve watchtower subscription renewal ${float(sub['amount_usd']):.2f}? [{sub_id}]",
            why=(f"subscription={sub_id} cron_job={sub.get('cron_job_id')} "
                f"correspondent={sub.get('correspondent_surface')}:"
                f"{sub.get('correspondent_address')}"),
            extra_payload={
                "ask_kind": self._RENEWAL_ASK_KIND,
                "tool_name": self._RENEWAL_ASK_TOOL,
                "subscription_id": sub_id,
                # M11: bind the grant to the amount the owner is approving, so a
                # later amount change can't be minted against a stale approval.
                "amount_usd": current_amount,
                "grant_consumed": False,
            },
            force=True,  # exact-key dedup above already did the real work
        )
        await self._push_owner_notice(
            tenant,
            f"🔐 Approval needed: watchtower subscription renewal "
            f"${float(sub['amount_usd']):.2f} for {sub.get('correspondent_surface')}:"
            f"{sub.get('correspondent_address')}\n"
            f"Reply /approve tap-{ask.id} or `polyrob owner promote tool_approval tap-{ask.id}`.",
        )
        return False
    async def _create_renewal_invoice(self, sub: dict) -> Optional[dict]:
        """Create the pending renewal invoice (metadata.subscription_id set)
        and best-effort deliver it to the correspondent when an active
        binding resolves. Returns the invoice dict, or None if creation was
        refused (cap/config — logged, never raised past this method)."""
        from modules.x402 import invoicing
        from modules.x402.invoicing import _emit as _invoicing_emit

        session_id = await self._resolve_correspondent_session(sub)
        try:
            inv = await invoicing.create_payment_request(
                user_id=sub["user_id"],
                session_id=session_id or "",
                amount_usd=float(sub["amount_usd"]),
                purpose=f"Watchtower subscription renewal ({sub.get('cron_job_id')})",
                correspondent_ref={"surface": sub.get("correspondent_surface"),
                                   "address": sub.get("correspondent_address")},
                subscription_id=sub["id"],
                db=self._db,
            )
        except ValueError as e:
            logger.warning("settlement watcher: renewal invoice refused for "
                           "subscription %s: %s", sub["id"], e)
            return None

        _invoicing_emit("subscription_renewal_invoiced", user_id=sub["user_id"], attrs={
            "subscription_id": sub["id"], "request_id": inv["request_id"],
            "amount_usd": inv["amount_usd"],
        })

        if session_id:
            deliver_corr = getattr(self.task_agent, "deliver_correspondent_data", None)
            if deliver_corr is not None:
                # Final-review cross-task fix (T11 C1 regression): this is a
                # PAYER-FACING payment instruction ("pay $X to <address>"), so it
                # MUST render the full-precision amount via the canonical
                # `format_invoice_amount` helper — NOT `:.2f`. When SUBSCRIPTIONS
                # + X402_SETTLE_ONCHAIN_DETECT compose, `create_payment_request`
                # forces sub-cent jitter on to disambiguate same-amount invoices
                # on-chain; a 2dp-rounded instruction ("$10.00" for a jittered
                # $10.0001 invoice) would make the payer pay $10.00, which the
                # oldest-first on-chain matcher then settles against a DIFFERENT,
                # older same-amount invoice (cross-subscription / cross-tenant
                # misdirected settlement) — exactly the C1 bug T11 closed on the
                # other three payer-facing surfaces (invoice_tool / cards /
                # artifact), missed here by T14.
                from modules.x402.artifact import format_invoice_amount
                text = (
                    f"Your watchtower subscription renewal is due: "
                    f"${format_invoice_amount(inv['amount_usd'])}. Pay to {inv['recipient']} "
                    f"({inv['chain']}), request {inv['request_id']}. "
                    "Reply once paid, or reach out with questions."
                )
                src = f"{sub.get('correspondent_surface')}:{sub.get('correspondent_address')}"
                delivered = await deliver_corr(
                    session_id, src, text,
                    {"kind_hint": "subscription_renewal_invoiced",
                     "request_id": inv["request_id"]})
                if not delivered:
                    logger.info("settlement watcher: renewal-invoice correspondent "
                               "delivery dropped for %s — ledger row is the record",
                               inv["request_id"])
        return inv
    async def _notify_suspended(self, sub: dict) -> None:
        """ONE owner notice + ONE correspondent notice (best-effort) + the
        SOLE `subscription_suspended` emission for this transition — the
        pure state flip in `subscriptions.subscriptions_to_suspend` does not
        emit, precisely so the event fires exactly once, here, alongside the
        notices."""
        from modules.x402.invoicing import _emit as _invoicing_emit

        sub_id = sub["id"]
        _invoicing_emit("subscription_suspended", user_id=sub.get("user_id") or "", attrs={
            "subscription_id": sub_id, "cron_job_id": sub.get("cron_job_id")})

        await self._push_owner_notice(
            sub.get("user_id") or "",
            f"Watchtower subscription {sub_id} for cron job "
            f"{sub.get('cron_job_id')} is SUSPENDED (unpaid past the "
            f"{sub.get('grace_days')}-day grace period) — its job will $0-skip "
            "until renewed, or cancel it with `polyrob owner sub cancel`.",
        )

        session_id = await self._resolve_correspondent_session(sub)
        if session_id:
            deliver_corr = getattr(self.task_agent, "deliver_correspondent_data", None)
            if deliver_corr is not None:
                src = f"{sub.get('correspondent_surface')}:{sub.get('correspondent_address')}"
                text = ("Your watchtower subscription has been suspended due to "
                       "non-payment. Renew to resume monitoring.")
                delivered = await deliver_corr(
                    session_id, src, text, {"kind_hint": "subscription_suspended"})
                if not delivered:
                    logger.info("settlement watcher: suspend correspondent notice "
                               "dropped for subscription %s", sub_id)
    async def _notify_subscription_apply_failed(self, inv: dict, result: Any) -> None:
        """Task 14 fix pass 2 (Finding 1): the underlying invoice settled
        on-chain, but its watchtower-subscription renewal extension could NOT
        be applied (``result`` is ``SettlementResult.REFUSED`` — an F3
        tenant mismatch — or ``UNKNOWN`` — the subscription_id no longer
        resolves to a real row) — money arrived, but ``paid_through`` was
        never extended. This must NEVER be reported as an ordinary
        ``payment_settled`` success: emits a DISTINCT
        ``subscription_apply_failed`` event, and an UNCONDITIONAL owner
        notice over the durable delivery rail (mirrors ``_notify_expired``'s
        rail so the owner learns of the anomaly even when the session isn't
        resident/wakeable) — an owner-actionable anomaly, not a silent
        success."""
        from modules.x402.invoicing import _emit

        request_id = inv.get("request_id")
        subscription_id = inv.get("subscription_id")
        user_id = inv.get("user_id") or ""
        amount = float(inv.get("amount_usd") or 0)
        reason = getattr(result, "value", str(result))

        _emit("subscription_apply_failed", user_id=user_id,
              session_id=inv.get("session_id") or "", attrs={
                  "request_id": request_id,
                  "subscription_id": subscription_id,
                  "reason": reason,
                  "amount_usd": amount,
              })
        logger.warning(
            "settlement watcher: subscription apply_settlement %s for invoice "
            "%s / subscription %s — payment SETTLED but the renewal "
            "extension was NOT applied; owner notified, wake claimed "
            "(terminal outcome, not retried further)", reason, request_id,
            subscription_id)

        owner_text = (
            f"Payment {request_id} for ${amount:.2f} settled, but its "
            f"watchtower subscription {subscription_id} could NOT be "
            f"extended ({reason}). The payment was received; please "
            "reconcile the subscription manually."
        )
        try:
            import core.surfaces.user_delivery as _ud
            container = getattr(self.task_agent, "container", None)
            await _ud.deliver_user_message(
                container, user_id, owner_text, source="subscriptions",
                session_id=inv.get("session_id") or None,
            )
        except Exception:
            logger.debug("settlement watcher: subscription-apply-failed owner "
                        "notice failed (fail-open)", exc_info=True)
