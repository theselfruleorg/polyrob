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
    #: 046 Phase 1 seams for the room-action branch, declared HERE because this
    #: is what uses them. A settled ``room_action`` invoice bought an EFFECT,
    #: not a session's attention, so it routes to
    #: `core.surfaces.room_actions.apply` instead of a self-wake — which needs a
    #: container (for the offer store) and an async performer (for the chat
    #: call). Assigned after construction by whoever wires the watcher.
    #:
    #: ⚠️ Absent, the branch records a CREDIT rather than pretending it applied.
    _room_container: Any = None
    _room_moderator: Any = None

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
        # 046: a room-action invoice bought an EFFECT, not a session's
        # attention. There is no session to wake and no owner to prompt to
        # "continue the work this payment was for" — actuate it instead.
        if (inv.get("kind") or "") == "room_action":
            await self._apply_room_action(inv)
            return

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

    async def _apply_room_action(self, inv: dict) -> None:
        """Actuate what a settled room-action invoice bought (046 Phase 1).

        ⚠️ Fail-open, and never silent. The money HAS arrived, so the offer is
        marked ``paid`` FIRST — if this process dies before the effect lands,
        the row still says paid and the obligation is visible rather than lost.
        A failure inside `apply` writes a CREDIT of its own; a failure to reach
        `apply` at all leaves the row at ``paid``, which the status snapshot and
        the next tick can both still see.
        """
        from modules.x402.invoicing import _emit

        offer_id = str((inv.get("room_action") or {}).get("offer_id") or "")
        _emit("payment_settled", user_id=inv.get("user_id") or "",
              session_id="", attrs={
                  "request_id": inv["request_id"],
                  "amount_usd": inv.get("amount_usd"),
                  "transaction_hash": inv.get("transaction_hash"),
                  "kind": "room_action", "offer_id": offer_id})
        if not offer_id:
            logger.warning("settlement watcher: room_action invoice %s carries "
                           "no offer_id — the payment is recorded but nothing "
                           "names what it bought", inv.get("request_id"))
            return
        container = getattr(self, "_room_container", None)
        if container is None:
            container = getattr(getattr(self, "task_agent", None),
                                "container", None)
        try:
            from core.surfaces import room_actions
            if not room_actions.mark_settled(container, offer_id):
                # ⚠️ `mark_settled` is now `pending -> paid` and ONLY that. A
                # payment against an EXPIRED (or withdrawn) offer used to
                # resurrect the row and apply an effect we had already told the
                # payer would not happen. Report it instead of acting on it.
                await self._report_late_room_payment(inv, offer_id)
                return
            result = await room_actions.apply(
                container, offer_id,
                perform_fn=getattr(self, "_room_moderator", None))
        except Exception:
            logger.warning("settlement watcher: room action %s could not be "
                           "applied (the offer stays PAID and shows as owed)",
                           offer_id, exc_info=True)
            return
        await self._deliver_room_action_result(inv, offer_id, result)

    async def _report_late_room_payment(self, inv: dict, offer_id: str) -> None:
        """A payment arrived for an offer that is no longer open.

        ⚠️ Money we now HOLD against nothing. It is an owner notice, and it is
        also said in the ROOM — the payer is in the room, not in the owner's DM,
        and silence would read as "it worked".
        """
        text = (f"⚠️ A payment arrived for offer {offer_id}, which is no longer "
                f"open. Nothing was applied. The owner has been notified and "
                f"will sort it out.")
        await self._post_to_room(offer_id, text)
        await self._push_owner_notice(
            inv.get("user_id") or "",
            f"Paid room action {offer_id} was PAID after it stopped being open "
            f"(invoice {inv.get('request_id')}). Nothing was applied and the "
            f"money is held.", source="room_actions", session_id=None)

    async def _post_to_room(self, offer_id: str, text: str) -> bool:
        """Put *text* in the room the offer belongs to. Never raises.

        ⚠️ This is the link that did not exist: a SUCCESSFUL effect was only
        `logger.info`, so the room — and the payer, who is only in the room —
        learned nothing at all. Rides the ONE outbound rail
        (`message_router`), cap-exempt because a receipt for money already
        taken is an obligation, not chatter.
        """
        try:
            container = (getattr(self, "_room_container", None)
                         or getattr(getattr(self, "task_agent", None),
                                    "container", None))
            if container is None:
                return False
            from core.surfaces.room_action_store import OfferStore, store_path
            from core.surfaces.room_actions import _data_dir
            row = OfferStore(store_path(_data_dir(container))).get(offer_id)
            if row is None:
                return False
            router = container.get_service("message_router")
            if router is None:
                logger.warning("room action %s: no message_router — the room "
                               "cannot be told (%s)", offer_id, text[:80])
                return False
            # ⚠️ Addressed by (surface, chat_id), NOT by a synthesized session
            # key. A room's live key carries its real `chat_type` (`supergroup`
            # for most Telegram rooms), so a key built here would miss the
            # binding — and this is out-of-band delivery anyway, the same shape
            # `cron/delivery.py` uses, which also means it is not subject to the
            # room's hourly reply cap. A receipt for money already taken is an
            # obligation, not chatter.
            ok = await router.send_message(str(row.chat_id), text,
                                           surface_id=row.surface)
            if not ok:
                logger.warning("room action %s: the room could not be told",
                               offer_id)
            return bool(ok)
        except Exception:
            logger.warning("room action %s: posting to the room failed",
                           offer_id, exc_info=True)
            return False

    async def _deliver_room_action_result(self, inv: dict, offer_id: str,
                                          result) -> None:
        """Put the receipt (or the honest failure) where the room can read it,
        and raise a failure to the owner.

        ⚠️ A credit is money we HOLD against an undelivered service, so it is an
        owner notice, not a log line.
        """
        try:
            text = getattr(result, "text", "") or ""
            # ⚠️ Both outcomes go to the ROOM. A success used to be a log line
            # only, so nobody present ever learned the effect landed; a failure
            # went to the OWNER, who is not the person owed a service.
            await self._post_to_room(offer_id, text)
            if getattr(result, "ok", False):
                logger.info("room action %s applied: %s", offer_id, text)
                return
            await self._push_owner_notice(
                inv.get("user_id") or "",
                f"Paid room action {offer_id} could not be applied — a credit "
                f"is owed. {text}", source="room_actions", session_id=None)
        except Exception:
            logger.warning("settlement watcher: could not deliver the room "
                           "action outcome for %s", offer_id, exc_info=True)

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
