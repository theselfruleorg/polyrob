"""Settlement watcher: the agent experiences
"I invoiced → I got paid" as one continuous piece of work — and a non-payment
is never silently dropped either (G-22).

A small ticker (same shape as the cron/goal tickers, wired through
``core/autonomy_runtime.start_autonomy``) that each tick:

0. (Task 11, Phase 2, gated ``X402_SETTLE_ONCHAIN_DETECT`` — default OFF) scans
   the treasury address on-chain for plain USDC transfers that arrived with NO
   facilitator/`POST /pay` at all — the "de-Coinbase" path a human payer takes
   when they just send funds to the address the invoice instructions show.
   A detected transfer that exactly matches a PENDING agent invoice's amount
   auto-settles it (oldest-first on a same-amount collision — see
   ``modules.x402.invoicing.match_pending_invoice_by_amount``); a transfer
   matching nothing emits ONE ``payment_unmatched`` event rather than being
   silently absorbed. Runs BEFORE the expiry sweep below so an invoice paid
   on-chain in the same tick it would otherwise lapse is settled, not expired.
   Off by default, and inert even when on unless the configured chain is
   mainnet (``base``) AND a treasury is configured — see ``_scan_onchain``.
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

# Mainnet-only gate for on-chain detection: `core.wallet.onchain._CHAIN` only
# carries a real (non-testnet) RPC entry for "base" — treat that name, and
# only that name, as mainnet. Anything else (unset, "base-sepolia", a typo)
# is refused rather than guessed at.
_MAINNET_CHAIN = "base"


def _scan_max_span() -> int:
    """Bounded per-tick block-range cap (`X402_SETTLEMENT_SCAN_MAX_SPAN`,
    default 5000) — a tick never scans more than this many blocks even after
    a long gap, so a resumed watcher can't issue one giant `eth_getLogs`."""
    from core.env import int_env
    return max(1, int_env("X402_SETTLEMENT_SCAN_MAX_SPAN", 5000))


def _scan_confirmations() -> int:
    """Confirmations buffer (`X402_SETTLEMENT_CONFIRMATIONS`, default 2) —
    blocks within this many of the chain head are never scanned yet (a
    just-mined block can still reorg out)."""
    from core.env import int_env
    return max(0, int_env("X402_SETTLEMENT_CONFIRMATIONS", 2))


class SettlementWatcher:
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

        onchain_settled = 0
        onchain_unmatched = 0
        try:
            onchain_settled, onchain_unmatched = await self._scan_onchain()
        except Exception:
            logger.warning("settlement watcher: on-chain scan failed", exc_info=True)

        expired = []
        settled = []
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

    async def _scan_onchain(self) -> tuple:
        """Task 11 (Phase 2): scan the treasury for new USDC transfers and
        auto-settle any exact-amount match. Returns (settled_count,
        unmatched_count) — always (0, 0) when detection is off, the chain
        isn't mainnet, no treasury is configured, or the RPC is unreachable
        (fail-open at every step; nothing here ever raises past this method,
        and the caller wraps it again defensively)."""
        from modules.x402 import invoicing
        if not invoicing.x402_settle_onchain_detect_enabled():
            return 0, 0

        from modules.x402.x402_integration import get_x402_config
        cfg = get_x402_config()
        treasury = (cfg.get("pay_to") or "").strip()
        chain = (cfg.get("network") or "").strip().lower()
        if not treasury or chain != _MAINNET_CHAIN:
            return 0, 0
        treasury_key = treasury.lower()

        call = self._rpc_call
        usdc_addr = self._usdc_addr
        if call is None:
            from core.wallet.onchain import _CHAIN, _rpc
            chain_cfg = _CHAIN.get(_MAINNET_CHAIN)
            if not chain_cfg:
                return 0, 0
            rpc_url, default_usdc, _sym = chain_cfg
            if usdc_addr is None:
                usdc_addr = default_usdc

            def call(method, params, _url=rpc_url):
                return _rpc(_url, method, params)
        elif usdc_addr is None:
            from core.wallet.onchain import USDC_BASE_MAINNET
            usdc_addr = USDC_BASE_MAINNET

        from modules.x402 import onchain_probe
        # M9: the probe does synchronous urllib I/O (eth_getLogs/eth_blockNumber
        # over up to 5000 blocks, up to a 4s timeout). Running it inline froze
        # the whole agent/API event loop every tick — offload to a thread so the
        # loop keeps serving /pay and other work during the RPC round-trip.
        head = await asyncio.to_thread(onchain_probe.get_head_block, call)
        if head is None:
            return 0, 0

        confirmations = _scan_confirmations()
        safe_head = head - confirmations
        last = await invoicing.get_scan_checkpoint(treasury_key, db=self._db)
        if last is None:
            # First run: seed the checkpoint near the head and scan NOTHING
            # this tick — never sweep from genesis. Detection picks up from
            # the NEXT tick onward.
            seed = max(0, safe_head)
            await invoicing.advance_scan_checkpoint(treasury_key, seed, db=self._db)
            return 0, 0

        from_block = last + 1
        if from_block > safe_head:
            return 0, 0  # nothing new past the confirmations buffer yet
        to_block = min(safe_head, from_block + _scan_max_span() - 1)

        # M9: same blocking-urllib offload as get_head_block above.
        transfers = await asyncio.to_thread(
            onchain_probe.scan_treasury_transfers,
            call, usdc_addr, treasury, from_block, to_block)
        settled, unmatched = await self._settle_or_flag(transfers, treasury_key)
        # Advance the checkpoint for the fully-processed range regardless of
        # match outcome — an unmatched/failed-settle transfer is recorded via
        # payment_unmatched, not by holding the scan cursor back.
        await invoicing.advance_scan_checkpoint(treasury_key, to_block, db=self._db)
        return settled, unmatched

    async def _settle_or_flag(self, transfers: list, treasury: str) -> tuple:
        """Task 11 review fix C2: EACH transfer is isolated in its own
        try/except so one failure can never block the rest of the batch NOR
        the scan checkpoint advance (which happens in the caller,
        unconditionally, after this returns) — a mid-batch exception here
        used to leave earlier settles committed but the loop silently
        aborted, and (worse) a re-scanned/consumed transfer could then settle
        a DIFFERENT, now-unrelated same-amount invoice on a later tick. A
        replayed `tx_hash` (one that already settled some invoice) is
        detected up front and skipped — never reapplied — via
        `invoicing.transaction_hash_already_settled`; `settle_payment_request`
        ALSO re-checks this immediately before the UPDATE, so the guard holds
        even if this loop's ordering ever changes."""
        from modules.x402 import invoicing

        settled = 0
        unmatched = 0
        for transfer in transfers:
            # H7: track a claim made for THIS transfer so a failure BETWEEN the
            # claim and the settle reverts it (pending->settling->stranded) —
            # cleared the moment the row reaches a terminal state.
            claimed_request_id: Optional[str] = None
            try:
                tx_hash = transfer.get("tx_hash")
                if tx_hash and await invoicing.transaction_hash_already_settled(
                        tx_hash, db=self._db):
                    # A given on-chain tx settles AT MOST ONE invoice EVER —
                    # this transfer was already applied (its original
                    # settlement row is the durable record); neither a new
                    # settlement nor a fresh payment_unmatched for it.
                    logger.info(
                        "settlement watcher: tx %s already settled an "
                        "invoice — skipping (replay guard)", tx_hash)
                    continue
                match = await invoicing.match_pending_invoice_by_amount(
                    transfer.get("amount_usd"), treasury, db=self._db)
                if not match:
                    await self._notify_unmatched(transfer, treasury)
                    unmatched += 1
                    continue
                request_id = match["request_id"]
                if not await invoicing.claim_for_settlement(request_id, db=self._db):
                    # Lost the race (already settling/settled/expired concurrently)
                    # — nothing to revert; this transfer just stays unflagged for
                    # THIS invoice, and is reported as unmatched for the tick.
                    await self._notify_unmatched(transfer, treasury)
                    unmatched += 1
                    continue
                claimed_request_id = request_id
                if await invoicing.settle_payment_request(
                        request_id, transaction_hash=tx_hash, db=self._db):
                    claimed_request_id = None  # terminal — nothing to revert
                    settled += 1
                    # The settled row now flows through the SAME
                    # settled_unnotified_invoices -> claim_wake -> _notify path as
                    # an owner/API settle — no new wake code needed.
                else:
                    await invoicing.revert_settlement_claim(request_id, db=self._db)
                    claimed_request_id = None
                    await self._notify_unmatched(transfer, treasury)
                    unmatched += 1
            except asyncio.CancelledError:
                # H7: CancelledError derives from BaseException, so the old bare
                # `except Exception` let a shutdown/force-cancel skip straight
                # past any revert — stranding a just-claimed invoice in
                # 'settling' forever. Honor cancellation immediately (re-raise);
                # a claim left behind is healed by the stale-settling reaper in
                # `tick_once` (age > 10min). Do NOT attempt an await-based revert
                # here — it would just be re-cancelled.
                logger.warning(
                    "settlement watcher: cancelled mid-settle for transfer %s "
                    "(a claim in 'settling', if any, is healed by the stale-"
                    "settling reaper next tick)", transfer.get("tx_hash"))
                raise
            except Exception:
                # A plain exception BETWEEN claim and settle (e.g.
                # settle_payment_request raised) used to strand the claim in
                # 'settling' — now revert it so the invoice stays payable. One
                # bad transfer must never block the rest of the batch nor the
                # checkpoint advance — log and move on.
                if claimed_request_id is not None:
                    try:
                        await invoicing.revert_settlement_claim(
                            claimed_request_id, db=self._db)
                    except Exception:
                        logger.warning(
                            "settlement watcher: could not revert stranded claim "
                            "%s after a settle failure — the stale-settling "
                            "reaper will heal it next tick", claimed_request_id,
                            exc_info=True)
                logger.warning(
                    "settlement watcher: settling transfer %s failed — "
                    "skipping (ledger rows are the record; checkpoint still "
                    "advances)", transfer.get("tx_hash"), exc_info=True)
                continue
        return settled, unmatched

    async def _notify_unmatched(self, transfer: dict, treasury: str) -> None:
        """A detected transfer that matches NO pending invoice must never be
        silently absorbed — the owner may have received an unexpected/
        overpaid/underpaid transfer (Task 11). Emits ONE durable
        `payment_unmatched` telemetry event; never settles anything."""
        from modules.x402.invoicing import _emit

        owner = ""
        try:
            from core.instance import resolve_owner_principal
            owner = resolve_owner_principal() or ""
        except Exception:
            owner = ""
        _emit("payment_unmatched", user_id=owner, session_id="", attrs={
            "tx_hash": transfer.get("tx_hash"),
            "from": transfer.get("from"),
            "amount_usd": transfer.get("amount_usd"),
            "block": transfer.get("block"),
            "treasury": treasury,
        })
        logger.warning(
            "settlement watcher: on-chain transfer %s ($%s from %s) matched NO "
            "pending invoice for treasury %s — no auto-settlement, owner should "
            "reconcile", transfer.get("tx_hash"), transfer.get("amount_usd"),
            transfer.get("from"), treasury)

    async def _sweep_stale_settling(self) -> int:
        """H7 stale-'settling' reaper: revert invoices stranded in 'settling'
        past 10 minutes back to 'pending' and notify the owner. A settle
        completes well within the 300s facilitator timeout, so a longer-lived
        'settling' row is a claim whose settling task was cancelled/crashed
        mid facilitator round-trip. The tx-hash uniqueness guard in
        `settle_payment_request` means a reverted-then-re-paid invoice can never
        double-settle on-chain. Fail-open (a sweep error never breaks the tick).
        Returns how many rows were reverted."""
        from modules.x402 import invoicing
        reverted = await invoicing.revert_stale_settling(
            max_age_seconds=600, db=self._db)
        for inv in reverted:
            try:
                invoicing._emit(
                    "payment_settling_reverted",
                    user_id=inv.get("user_id") or "",
                    session_id=inv.get("session_id") or "", attrs={
                        "request_id": inv.get("request_id"),
                        "amount_usd": inv.get("amount_usd")})
                owner_text = (
                    f"Invoice {inv.get('request_id')} for "
                    f"${float(inv.get('amount_usd') or 0):.2f} was stuck mid-"
                    "settlement for over 10 minutes and has been reset to "
                    "payable. If a payment DID clear on-chain, reconcile it — "
                    "the on-chain tx-hash guard prevents a double-settle.")
                await self._push_owner_notice(inv.get("user_id") or "", owner_text)
            except Exception:
                logger.debug(
                    "settlement watcher: stale-settling owner notice failed for "
                    "%s (fail-open)", inv.get("request_id"), exc_info=True)
        return len(reverted)

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
            # Task 15 (Phase 4): the settlement notice's target IS an
            # identifiable payer (an ACTIVE correspondent channel) — exactly
            # the anti-sybil "who paid" signal ERC-8004 payment-backed
            # feedback needs. Fail-open: an 8004 error must NEVER affect the
            # settlement notice above (already delivered by this point).
            try:
                await self._maybe_offer_payment_feedback(inv, session_id, cref)
            except Exception:
                logger.warning(
                    "settlement watcher: eip8004 payment-feedback hook failed "
                    "for %s (fail-open, settlement unaffected)",
                    inv.get("request_id"), exc_info=True)
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

    def _reputation_mgr(self):
        """The ERC-8004 `ReputationManager` the payment-feedback-authorization
        hook uses. An injected test double (constructor `reputation_manager=`)
        wins; production lazily builds a real one sharing this watcher's
        `db` — mirrors `_goal_board()`'s lazy-build pattern."""
        if self._reputation_manager_override is not None:
            return self._reputation_manager_override
        from modules.eip8004.reputation import ReputationManager
        return ReputationManager(db=self._db)

    async def _maybe_offer_payment_feedback(
        self, inv: dict, session_id: str, cref: dict,
    ) -> None:
        """Task 15 (Phase 4): offer the payer a signed ERC-8004 feedback
        AUTHORIZATION + payment proof once their invoice settles — the
        anti-sybil "verified paying customer" signal ERC-8004 reputation was
        designed for. This method NEVER submits feedback on the payer's
        behalf (that would be fabricated reputation) — it only creates the
        redeemable authorization and, best-effort, tells the payer it exists.

        Gated `EIP8004_PAYMENT_FEEDBACK` (rides `EIP8004_ENABLED` — see
        `core.config_policy.eip8004_payment_feedback_enabled`). Requires a
        verifiable on-chain transaction hash: a settlement with none (e.g. a
        manually attested ``settled_no_tx`` invoice) has nothing to prove, so
        is silently skipped rather than offering a hollow proof. The caller
        (`_notify`) already guarantees an identifiable payer (an ACTIVE
        correspondent channel) and wraps this whole call fail-open.
        """
        from core.config_policy import eip8004_payment_feedback_enabled
        if not eip8004_payment_feedback_enabled():
            return
        tx_hash = inv.get("transaction_hash")
        if not tx_hash:
            return

        from modules.x402 import invoicing
        row = await invoicing.get_payment_request(inv["request_id"], db=self._db)
        if not row:
            return

        from modules.eip8004.payment_proof import proof_from_settled_invoice
        proof = proof_from_settled_invoice({
            "request_id": inv["request_id"],
            "tx_hash": tx_hash,
            "chain": row.get("chain"),
            "recipient": row.get("recipient"),
            "payer_address": cref.get("address"),
        })

        manager = self._reputation_mgr()
        auth = await manager.create_feedback_auth(
            client_address=cref.get("address"), task_id=inv["request_id"])

        from modules.x402.invoicing import _emit as _invoicing_emit
        _invoicing_emit(
            "payment_feedback_authorized", user_id=inv.get("user_id") or "",
            session_id=session_id, attrs={
                "request_id": inv["request_id"], "agent_id": auth.agentId})

        deliver_corr = getattr(self.task_agent, "deliver_correspondent_data", None)
        if deliver_corr is None:
            return
        src = f"{cref.get('surface', '')}:{cref.get('address', '')}"
        text = (
            "You can leave verified feedback for this payment on the "
            f"ERC-8004 Reputation Registry: agentId={auth.agentId}, "
            f"nonce={auth.nonce}, expiresAt={auth.expiresAt}, "
            f"signature={auth.signature}. Submit a score 0-100 to "
            "/eip8004/reputation/feedback with this authorization and proof "
            f"of payment (tx {proof.txHash}, chain {proof.chainId})."
        )
        delivered = await deliver_corr(
            session_id, src, text,
            {"kind_hint": "payment_feedback_authorization",
             "request_id": inv["request_id"]})
        if not delivered:
            logger.info(
                "settlement watcher: eip8004 feedback-authorization offer "
                "dropped for %s — the authorization was still created "
                "(not resent; not durably tracked beyond this log)",
                inv["request_id"])

    async def _notify_expired(self, inv: dict) -> None:
        """Non-payment escalation (G-22): notify whoever's waiting on the
        session side (correspondent DATA or an owner self-wake — the SAME
        rails :meth:`_notify` uses for settlement), PLUS an unconditional
        owner notification over the durable delivery rail so a non-payment is
        never invisible to the owner even when the session can't be woken.

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
        session_id = inv.get("session_id") or ""
        if session_id:
            # A correspondent-linked invoice is delivered as DATA on the
            # correspondent rail — never the owner "obey" queue — mirroring
            # the settlement path's tenant-safety rationale.
            cref = inv.get("correspondent_ref")
            delivered_via_correspondent = False
            if cref and self._correspondent_active(cref):
                deliver_corr = getattr(self.task_agent, "deliver_correspondent_data", None)
                if deliver_corr is not None:
                    src = f"{cref.get('surface', '')}:{cref.get('address', '')}"
                    delivered = await deliver_corr(
                        session_id, src, text,
                        {"kind_hint": "payment_expired", "request_id": request_id})
                    if not delivered:
                        logger.info("expiry correspondent-data for %s dropped — "
                                    "ledger row is the record", request_id)
                    delivered_via_correspondent = True

            if not delivered_via_correspondent:
                deliver = getattr(self.task_agent, "deliver_self_wake", None)
                if deliver is not None:
                    delivered = await deliver(
                        session_id, inv.get("user_id") or "", text,
                        metadata={"kind_hint": "payment_expired",
                                  "request_id": request_id},
                    )
                    if not delivered:
                        logger.info("expiry wake for %s dropped (self-wake disabled/"
                                    "budget/non-resident) — ledger row is the record",
                                    request_id)

        # PLUS: an owner notification over the durable delivery rail,
        # UNCONDITIONALLY — a non-payment must reach the owner even when the
        # session isn't resident/wakeable at all. The rail is itself fail-open
        # with a durable owner_notice fallback, so this never blocks the tick.
        try:
            import core.surfaces.user_delivery as _ud
            container = getattr(self.task_agent, "container", None)
            owner_text = f"Invoice {request_id} for ${amount:.2f} expired unpaid."
            await _ud.deliver_user_message(
                container, inv.get("user_id") or "", owner_text,
                source="x402_invoice", session_id=session_id or None,
            )
        except Exception:
            logger.debug("settlement watcher: owner notice failed for %s (rail has "
                        "its own durable fallback — ledger row is the record)",
                        request_id, exc_info=True)

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

    # --- Task 14 (Phase 3 R5): watchtower subscription renewal + lapse ------

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

    async def _push_owner_notice(self, user_id: str, text: str) -> None:
        """Best-effort owner notification over the SAME durable delivery rail
        `_notify_expired`'s owner notice uses. Never raises."""
        if not user_id or not text:
            return
        try:
            import core.surfaces.user_delivery as _ud
            container = getattr(self.task_agent, "container", None)
            await _ud.deliver_user_message(container, user_id, text, source="subscriptions")
        except Exception:
            logger.debug("settlement watcher: subscription owner notice failed "
                        "(fail-open)", exc_info=True)

    async def _resolve_correspondent_session(self, sub: dict) -> Optional[str]:
        """The session_id an ACTIVE correspondent binding for this
        subscription's (surface, address) resolves to — or None when
        correspondent access is off, unconfigured, or there is no active
        binding yet. Mirrors `_correspondent_active` but returns the routable
        session_id instead of a bool (the caller needs it to actually
        deliver). Fail-open to None."""
        try:
            from agents.task.surface_config import SurfaceConfig
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
