"""Settlement watcher: the agent experiences
"I invoiced → I got paid" as one continuous piece of work — and a non-payment
is never silently dropped either (G-22).

A small ticker (same shape as the cron/goal tickers, wired through
``core/autonomy_runtime.start_autonomy``) that each tick:

0. (Task 11, Phase 2, gated ``X402_SETTLE_ONCHAIN_DETECT`` — default OFF, ON under
   effective AUTONOMY_MODE=autonomous) scans
   the treasury address on-chain for plain USDC transfers that arrived with NO
   facilitator/`POST /pay` at all — the "de-Coinbase" path a human payer takes
   when they just send funds to the address the invoice instructions show.
   A detected transfer that exactly matches a PENDING agent invoice's amount
   auto-settles it (oldest-first on a same-amount collision — see
   ``modules.x402.invoicing.match_pending_invoice_by_amount``); a transfer
   matching nothing emits ONE ``payment_unmatched`` event rather than being
   silently absorbed. Runs BEFORE the expiry sweep below so an invoice paid
   on-chain in the same tick it would otherwise lapse is settled, not expired.
   Inert unless the configured chain is scannable (``base`` mainnet or
   ``base-sepolia``) AND a treasury is resolved; the RPC's ``eth_chainId`` is
   verified against that chain before any log read, so a wrong-network
   endpoint refuses to scan instead of reporting a false "nobody paid" —
   see ``_scan_onchain`` / ``_verify_scan_network``.
1. expires pending invoices past their deadline (``payment_expired`` events);
2. finds settled-but-unnotified agent invoices and re-enters each one's
   originating session via the existing self-wake rail
   (``TaskAgent.deliver_self_wake`` — kind is always ``self_wake``; the payment
   context rides in ``metadata`` per the UP-12/W1 contract), then emits the
   first-class ``payment_settled`` event and marks the row notified;
3. finds expired-but-unnotified agent invoices (G-22) and, for each: delivers
   a session-side notice (correspondent DATA when an active correspondent_ref
   is linked, else an owner self-wake — mirroring step 2's rails) AND a
   separate owner notification over the durable user-delivery rail
   (``core/surfaces/user_delivery.py``), so the owner learns of a non-payment
   even when the session isn't resident/wakeable. The ``payment_expired``
   event itself was already emitted at expiry time (step 1); this step is
   notification-only;
4. (Task 14, Phase 3 R5, gated ``SUBSCRIPTIONS_ENABLED`` — default OFF)
   watchtower subscriptions: a settled invoice carrying ``metadata.subscription_id``
   (detected inline in step 2, above) extends that subscription's
   ``paid_through`` via ``modules.x402.subscriptions.apply_settlement``
   (idempotent, keyed on the invoice's ``request_id`` — Task 14 fix pass 2:
   the ledger claim + the extension now land in ONE transaction, and the
   call returns a ``SettlementResult`` this tick branches on —
   ``APPLIED``/``ALREADY_APPLIED`` proceed to the normal wake;
   ``REFUSED``/``UNKNOWN`` claim the wake but deliver a DISTINCT
   ``subscription_apply_failed`` owner anomaly notice instead of a
   misleading "settled" one; an exception withholds the wake and retries
   next tick). Separately, this step
   creates the NEXT renewal invoice ahead of `paid_through` (respecting
   ``PAYMENT_APPROVAL_MODE`` — ``auto`` invoices immediately, ``approve``
   queues a durable owner ``tool_approval`` ask via the EXISTING
   ``agents.task.goals.board.GoalBoard`` and invoices only once approved on a
   later tick), and moves a lapsed subscription through
   ``active -> grace -> suspended`` (one owner + one correspondent notice on
   suspend). ``cron/runner.py`` consults the resulting status to $0-skip a
   lapsed subscription's cron job. All-flag-off is byte-identical: the
   subscriptions table is never even queried.

Both wakes are best-effort (SELF_WAKE_ENABLED off / non-resident session /
budget exhausted → dropped): the settled/expired row + its first-class event
remain the durable record either way, so a row is marked notified exactly
once and the agent/owner can always reconcile from the ledger. Every step is
fail-open — a watcher error never breaks the autonomy runtime.
"""
import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


from modules.x402.settlement_scan import (  # noqa: F401  (helpers re-exported for callers/tests)
    SettlementScanMixin,
    _MAINNET_CHAIN,
    _SEPOLIA_RPC_DEFAULT,
    _known_swap_router_addresses,
    _redact_rpc,
    _resolve_scan_target,
    _scan_confirmations,
    _scan_max_span,
    solana_settle_enabled,
)
from modules.x402.settlement_notify import SettlementNotifyMixin
from modules.x402.settlement_subscriptions import SettlementSubscriptionsMixin
from modules.x402.settlement_reputation import SettlementReputationMixin


class SettlementWatcher(SettlementScanMixin, SettlementNotifyMixin,
                        SettlementSubscriptionsMixin, SettlementReputationMixin):
    """Poll pending/settled agent invoices; deliver settlement wakes."""

    #: ask_kind/tool_name pair identifying a durable owner ask created for a
    #: PAYMENT_APPROVAL_MODE=approve subscription renewal — reuses the SAME
    #: `tool_approval` ask machinery `tools/controller/approval_queue.py`
    #: uses, so `polyrob owner pending` / `owner promote tool_approval <id>`
    #: handle it with ZERO new owner-facing surface.
    _RENEWAL_ASK_KIND = "tool_approval"
    _RENEWAL_ASK_TOOL = "subscription_renewal"

    def __init__(self, task_agent: Any, *, db=None, interval_seconds: int = 60,
                 rpc_call=None, usdc_addr: Optional[str] = None, goal_board=None,
                 reputation_manager=None):
        """``rpc_call``/``usdc_addr`` are the on-chain-detection test seam
        (Task 11): ``rpc_call(method: str, params: list) -> Any`` returning
        the JSON-RPC ``result``. When left None (production default), a
        real call is wired lazily in `_scan_onchain` against the SAME Base
        RPC + USDC contract `core/wallet/onchain.py` already trusts — never
        constructed eagerly here, so a watcher with detection OFF never even
        imports the on-chain module.

        ``goal_board`` (Task 14): test/injection seam for the durable-ask
        store the subscription-renewal approve-mode flow rides
        (`agents.task.goals.board.GoalBoard`). Production lazily builds one
        against `core.runtime_config.get_data_root()`'s `goals.db` — the SAME
        file the goal dispatcher/owner CLI already use — so an injected
        instance in tests never touches a real data home.

        ``reputation_manager`` (Task 15, Phase 4): test/injection seam for the
        ERC-8004 `ReputationManager` the payment-feedback-authorization hook
        uses (`_maybe_offer_payment_feedback`). Production lazily builds a
        real one (`modules.eip8004.reputation.ReputationManager`), sharing
        this watcher's `db`."""
        self.task_agent = task_agent
        self._db = db
        self.interval_seconds = interval_seconds
        self._rpc_call = rpc_call
        self._usdc_addr = usdc_addr
        self._goal_board_override = goal_board
        self._reputation_manager_override = reputation_manager

    async def tick_once(self) -> dict:
        from modules.x402 import invoicing
        from core.autonomy_control import allows
        if not (_dec := allows("settlement_scan")).allowed:
            return {"skipped": _dec.reason}  # 031 owner pause
        onchain_settled = onchain_unmatched = 0
        try:
            onchain_settled, onchain_unmatched = await self._scan_onchain()
        except Exception:
            logger.warning("settlement watcher: on-chain scan failed", exc_info=True)

        # Phase 4: the Solana pass runs BESIDE the EVM one, never instead of it.
        # Its own flag, its own matching strategy (reference, not amount), and
        # its own try/except so a Solana RPC problem cannot stop EVM settlement.
        try:
            svm_settled, svm_unmatched = await self._scan_solana()
            onchain_settled += svm_settled
            onchain_unmatched += svm_unmatched
        except Exception:
            logger.warning("settlement watcher: solana scan failed", exc_info=True)

        expired, settled = [], []
        try:
            expired = await invoicing.expire_stale_requests(db=self._db)
        except Exception:
            logger.warning("settlement watcher: expiry sweep failed", exc_info=True)
        # H7: heal invoices stranded in 'settling' (a claim that never completed
        # because the settling task was cancelled/crashed mid facilitator
        # round-trip) — nothing else ever re-checks 'settling'.
        settling_reverted = 0
        try:
            settling_reverted = await self._sweep_stale_settling()
        except Exception:
            logger.warning("settlement watcher: stale-settling sweep failed", exc_info=True)
        try:
            settled = await invoicing.settled_unnotified_invoices(db=self._db)
        except Exception:
            logger.warning("settlement watcher: settled poll failed", exc_info=True)

        from modules.x402 import subscriptions as subs
        subs_enabled = subs.subscriptions_enabled()

        notified = 0
        for inv in settled:
            try:
                # Task 14: apply a subscription renewal's settlement BEFORE the
                # wake claim-then-notify below — it has its OWN idempotency key
                # (subscription_applied_settlements, keyed on request_id), so it
                # is safe to attempt regardless of which watcher process (if
                # any) wins the wake_delivered claim, and must never be skipped
                # just because a concurrent process already claimed the wake.
                if inv.get("subscription_id"):
                    if not subs_enabled:
                        # M6: a settled renewal invoice while SUBSCRIPTIONS_ENABLED
                        # is OFF must NEVER deliver the ordinary "settled, continue"
                        # wake nor burn wake_delivered — doing so would flip
                        # wake_delivered=true and permanently strand the paid
                        # renewal's paid_through extension (settled_unnotified_
                        # invoices only ever returns wake_delivered=false rows).
                        # metadata.subscription_id is durable evidence this is a
                        # renewal; WITHHOLD the wake (leave wake_delivered=false so
                        # a re-enabled watcher can still apply the extension via the
                        # normal path — preserving retryability) and emit a DISTINCT
                        # anomaly event instead of the ordinary settled one.
                        from modules.x402.invoicing import _emit as _inv_emit
                        _inv_emit(
                            "subscription_apply_failed",
                            user_id=inv.get("user_id") or "",
                            session_id=inv.get("session_id") or "", attrs={
                                "request_id": inv.get("request_id"),
                                "subscription_id": inv.get("subscription_id"),
                                "reason": "subscriptions_disabled",
                                "amount_usd": float(inv.get("amount_usd") or 0)})
                        logger.warning(
                            "settlement watcher: settled renewal invoice %s for "
                            "subscription %s but SUBSCRIPTIONS_ENABLED is off — "
                            "withholding the wake (retryable once re-enabled), NOT "
                            "delivering the ordinary settled wake",
                            inv.get("request_id"), inv.get("subscription_id"))
                        continue
                    try:
                        result = await subs.apply_settlement(
                            inv["subscription_id"], inv["request_id"], db=self._db)
                    except Exception:
                        # Money-critical: do NOT fall through to claim_wake below.
                        # claim_wake flips metadata.wake_delivered=true, and
                        # settled_unnotified_invoices only ever returns rows with
                        # wake_delivered=false — so if we let this invoice get
                        # claimed/notified while its renewal extension failed to
                        # apply, the row would NEVER be retried again and the
                        # paid_through extension would be lost permanently, with
                        # no error surfaced anywhere but this log line. `continue`
                        # leaves wake_delivered false so this exact invoice is
                        # re-attempted on the NEXT tick; apply_settlement's own
                        # idempotency ledger (subscription_applied_settlements,
                        # keyed on request_id) makes that retry safe — the ledger
                        # INSERT + paid_through UPDATE now land in ONE
                        # transaction (Task 14 fix pass 2, Finding 1), so a
                        # retry after a raised exception never sees a stale
                        # partially-applied ledger row. This only skips THIS one
                        # invoice for THIS tick — every other invoice in
                        # `settled` is still processed, so a single persistently-
                        # failing subscription can never livelock the tick (it
                        # just keeps retrying next tick, forever, same shape as
                        # the on-chain transfer retry in `_settle_or_flag`; no
                        # backoff/escalation exists for this path today).
                        logger.warning(
                            "settlement watcher: subscription apply_settlement "
                            "failed for %s / %s — NOT marking wake delivered, "
                            "will retry next tick (renewal extension not yet "
                            "applied)", inv.get("subscription_id"),
                            inv.get("request_id"), exc_info=True)
                        continue

                    if result in (subs.SettlementResult.REFUSED, subs.SettlementResult.UNKNOWN):
                        # Task 14 fix pass 2 (Finding 1): these are TERMINAL —
                        # not retryable — outcomes where the on-chain payment
                        # DID settle but the subscription extension could NOT
                        # be applied (F3 tenant mismatch, or the
                        # subscription_id no longer resolves). Retrying
                        # forever would never change the outcome, so claim the
                        # wake to stop the retry loop, but this must NEVER be
                        # silently reported as an ordinary "settled, continue
                        # your work" wake — deliver a DISTINCT owner-actionable
                        # anomaly notice + event instead of the normal one.
                        if await invoicing.claim_wake(inv["request_id"], db=self._db):
                            await self._notify_subscription_apply_failed(inv, result)
                            notified += 1
                        continue
                    # APPLIED or ALREADY_APPLIED: the extension is guaranteed
                    # to have landed (this call, or a prior one, atomically) —
                    # safe to fall through to the normal settlement wake below.
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

        expired_pending = []
        try:
            expired_pending = await invoicing.expired_unnotified_invoices(db=self._db)
        except Exception:
            logger.warning("settlement watcher: expired poll failed", exc_info=True)

        expired_notified = 0
        for inv in expired_pending:
            try:
                # Same claim-then-notify shape as settlement, over the SAME
                # wake_delivered flag (see invoicing.claim_expiry_wake) — safe
                # because this query is already status-partitioned to 'expired'
                # rows only, so it can never race a settlement claim.
                if not await invoicing.claim_expiry_wake(inv["request_id"], db=self._db):
                    continue
                await self._notify_expired(inv)
                expired_notified += 1
            except Exception:
                logger.warning("settlement watcher: expiry notify failed for %s "
                               "(claim consumed — ledger row is the record)",
                               inv.get("request_id"), exc_info=True)

        sub_stats = {"subscription_renewals_invoiced": 0, "subscription_grace": 0,
                     "subscription_suspended": 0}
        try:
            sub_stats = await self._process_subscriptions()
        except Exception:
            logger.warning("settlement watcher: subscription processing failed", exc_info=True)

        return {"expired": len(expired), "settled_notified": notified,
                "expired_notified": expired_notified,
                "settling_reverted": settling_reverted,
                "onchain_settled": onchain_settled,
                "onchain_unmatched": onchain_unmatched,
                **sub_stats}


    # --- Task 14 (Phase 3 R5): watchtower subscription renewal + lapse ------


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
