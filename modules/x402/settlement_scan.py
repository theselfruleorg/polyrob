"""On-chain settlement detection: chain verification, the Base/Solana treasury scans, exact-amount matching, unmatched-transfer notices and the stale-settling sweep. Also hosts the scan helpers (`_resolve_scan_target`, `_redact_rpc`, spans/confirmations) the watcher module re-exports.

Split out of ``modules/x402/settlement_watcher.py`` (S6, 2026-08-29). A mixin over
``SettlementWatcher`` — it relies on the host for ``task_agent``, ``_db``, the injection
seams (``_rpc_call``/``_usdc_addr``/``_goal_board_override``/``_reputation_manager_override``)
and the module ``logger``; behaviour is byte-identical to the pre-split class.
"""
import logging
from typing import Any, Optional
import asyncio

from core.security.redaction import redact_url

logger = logging.getLogger("modules.x402.settlement_watcher")


# Scannable-chain gate for on-chain detection (W1.2, 2026-08-21): mainnet
# "base" plus the base-sepolia testnet (so the Tier-1 acceptance run can
# settle on testnet per the house test rule). Anything else (unset, a typo,
# an unsupported chain) is refused rather than guessed at. Kept as a module
# constant for the legacy name too — external callers/tests referenced it.
_MAINNET_CHAIN = "base"
# The sepolia RPC deliberately does NOT live in core/wallet/chains.py — the
# money-chain registry keeps its mainnet-only semantics; the watcher carries
# its own testnet default, overridable via X402_SETTLEMENT_RPC.
_SEPOLIA_RPC_DEFAULT = "https://sepolia.base.org"
def _known_swap_router_addresses(chain: str) -> frozenset:
    """Addresses our OWN `defi_trade` tool sends funds through on `chain`
    (the local Uniswap-v3 router + the LI.FI aggregator spender, per
    `core.wallet.chains`). A token sell's proceeds land back in the treasury
    as a plain inbound USDC Transfer FROM one of these — on-chain that is
    indistinguishable from a stray payment, so without this check every sell
    fires a `payment_unmatched` owner notice for money the agent itself just
    moved (owner-reported 2026-08-27: a burst of "unmatched payment" pings
    that lined up exactly with a run of ledger-recorded sells).

    ⚠️ This set NARROWS which transfers get correlated; it never decides one on
    its own. These are shared public contracts — every wallet on the chain uses
    them — so the address is only the trigger for the real test, an exact hash
    match against our own recorded trades (`core.wallet.trade_index`). Skipping
    on the address alone silently swallowed genuine payments routed through the
    same router."""
    if not chain:
        return frozenset()
    try:
        from core.wallet import chains
        row = chains.get(chain)
    except Exception:
        return frozenset()
    if row is None:
        return frozenset()
    return frozenset(
        a.lower() for a in (row.univ3_router, row.aggregator_spender) if a)
#: An RPC endpoint for logging — scheme + host only. A provider URL carries its
#: credential in the PATH (`https://<net>.g.alchemy.com/v2/<api-key>`) or the
#: query, where every name-keyed secret scrubber misses it. Observed live: the
#: detection-active line published a real Alchemy key to the journal.
#:
#: The implementation now lives in `core.security.redaction` because a SECOND
#: site composed the same URL and logged it (`tools/defi/providers/
#: alchemy_index.py`) — a private helper is a fix the next site cannot import.
#: The name is kept: `settlement_watcher` re-exports it.
_redact_rpc = redact_url
def _resolve_scan_target(chain: str):
    """(rpc_url, usdc_addr, expected_chain_id) for a scannable chain, or None
    when the chain is not scannable. Precedence (W1.3): `X402_SETTLEMENT_RPC`
    (watcher-specific pin, any chain) > `DEFI_EVM_RPC_BASE` via
    `rpc_url_for_chain` (mainnet only — parity with balance reads, which
    already honored it while the scan did not) > built-in default.

    The chain id travels with the target because the URL and the USDC address
    come from DIFFERENT sources (an env pin vs the configured chain name) and
    nothing else would catch them disagreeing — see `_verify_scan_network`.
    """
    import os
    from core.wallet.onchain import (USDC_BASE_MAINNET, USDC_BASE_SEPOLIA,
                                     rpc_url_for_chain)
    from modules.x402.artifact import (_BASE_CHAIN_ID, _BASE_SEPOLIA_CHAIN_ID,
                                       _is_sepolia_chain)
    override = os.getenv("X402_SETTLEMENT_RPC", "").strip()
    if chain == _MAINNET_CHAIN:
        url = override or rpc_url_for_chain(_MAINNET_CHAIN)
        return (url, USDC_BASE_MAINNET, _BASE_CHAIN_ID) if url else None
    if _is_sepolia_chain(chain):
        return (override or _SEPOLIA_RPC_DEFAULT, USDC_BASE_SEPOLIA,
                _BASE_SEPOLIA_CHAIN_ID)
    return None
def solana_settle_enabled() -> bool:
    """Its OWN flag, ANDed with the master detection switch.

    Arming EVM on-chain detection must not silently arm a second chain's money
    path — a different rail, a different signer family and a different matching
    strategy deserve a deliberate second decision.
    """
    import os
    if not os.getenv("X402_SETTLE_ONCHAIN_DETECT", "false").strip().lower() in (
            "1", "true", "yes", "on"):
        return False
    return os.getenv("X402_SOLANA_SETTLE", "false").strip().lower() in (
        "1", "true", "yes", "on")
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


class SettlementScanMixin:
    async def _verify_scan_network(self, call, expected_chain_id: int,
                                   rpc_url: str, chain: str) -> bool:
        """Confirm the RPC really serves the chain the USDC address belongs to.

        The URL and the asset address come from different sources (an env pin
        vs `X402_DEFAULT_CHAIN`), so they can disagree — a sepolia pin left in
        place after a testnet run, or a provider URL that silently points at
        another network. Scanning the wrong chain for a mainnet USDC contract
        returns a VALID EMPTY log set, which is indistinguishable from "nobody
        paid": the checkpoint advances, detection reports healthy, and a real
        payment is never seen (the payer then gets an "expired" notice). One
        `eth_chainId` per RPC URL closes that off.

        Verified once per (watcher, url) — cached, so this costs one extra call
        at startup, not one per tick. An unverifiable RPC returns False (skip
        the tick, hold the cursor) rather than being trusted."""
        cache = getattr(self, "_verified_rpc_urls", None)
        if cache is None:
            cache = self._verified_rpc_urls = {}
        cached = cache.get(rpc_url)
        if cached is not None:
            return cached

        try:
            raw = await asyncio.to_thread(call, "eth_chainId", [])
            actual = int(str(raw), 16) if isinstance(raw, str) else int(raw)
        except Exception as e:
            # Do NOT cache: a transient RPC failure must retry next tick.
            logger.warning(
                "x402 on-chain detection: could not verify the network of %s "
                "(%s) — skipping this tick and retrying; detection is INACTIVE "
                "until the endpoint answers eth_chainId", _redact_rpc(rpc_url), e)
            return False

        if actual != expected_chain_id:
            cache[rpc_url] = False
            logger.error(
                "x402 on-chain detection DISABLED: RPC %s serves chain id %s but "
                "X402_DEFAULT_CHAIN=%r expects %s. Scanning it would read the "
                "wrong network and silently miss real payments. Fix "
                "X402_SETTLEMENT_RPC/DEFI_EVM_RPC_BASE or X402_DEFAULT_CHAIN.",
                _redact_rpc(rpc_url), actual, chain, expected_chain_id)
            return False

        cache[rpc_url] = True
        return True
    async def _scan_onchain(self) -> tuple:
        """Task 11 (Phase 2): scan the treasury for new USDC transfers and
        auto-settle any exact-amount match. Returns (settled_count,
        unmatched_count) — always (0, 0) when detection is off, the chain
        isn't scannable, no treasury is configured, or the RPC is unreachable
        (fail-open at every step; nothing here ever raises past this method,
        and the caller wraps it again defensively)."""
        from modules.x402 import invoicing
        if not invoicing.x402_settle_onchain_detect_enabled():
            return 0, 0

        from modules.x402.x402_integration import get_x402_config
        cfg = get_x402_config()
        treasury = (cfg.get("pay_to") or "").strip()
        chain = (cfg.get("network") or "").strip().lower()
        if not treasury:
            return 0, 0
        target = _resolve_scan_target(chain)
        if target is None:
            # Detection is ON but the chain can't be scanned — never a silent
            # no-op (the pre-W1.2 sepolia trap): say so once per watcher.
            if not getattr(self, "_scan_target_warned", False):
                self._scan_target_warned = True
                logger.warning(
                    "X402_SETTLE_ONCHAIN_DETECT is on but X402_DEFAULT_CHAIN=%r "
                    "is not a scannable chain (supported: base, base-sepolia) — "
                    "on-chain detection is inactive", chain)
            return 0, 0
        rpc_url, default_usdc, expected_chain_id = target
        if not getattr(self, "_scan_target_logged", False):
            self._scan_target_logged = True
            logger.info("x402 on-chain detection active: chain=%s rpc=%s",
                        chain, _redact_rpc(rpc_url))
        treasury_key = treasury.lower()

        call = self._rpc_call
        usdc_addr = self._usdc_addr
        if usdc_addr is None:
            usdc_addr = default_usdc
        if call is None:
            from core.wallet.onchain import _rpc

            def call(method, params, _url=rpc_url):
                return _rpc(_url, method, params)

        if not await self._verify_scan_network(call, expected_chain_id, rpc_url, chain):
            return 0, 0

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
        if transfers is None:
            # The range was NOT scanned (RPC error). Hold the cursor so the next
            # tick retries it — advancing here would burn the range forever,
            # since advance_scan_checkpoint refuses to regress. A payer's real
            # transfer inside it would never be enumerated, so not even
            # payment_unmatched would fire (audit 2026-08-07 #1, Critical).
            logger.warning(
                "x402 settlement scan: blocks %s..%s UNSCANNED (rpc failure) — "
                "checkpoint held at %s, will retry next tick",
                from_block, to_block, last)
            return 0, 0
        settled, unmatched = await self._settle_or_flag(transfers, treasury_key, chain)
        # Advance the checkpoint for the fully-processed range regardless of
        # match outcome — an unmatched/failed-settle transfer is recorded via
        # payment_unmatched, not by holding the scan cursor back.
        await invoicing.advance_scan_checkpoint(treasury_key, to_block, db=self._db)
        return settled, unmatched
    async def _scan_solana(self) -> tuple:
        """Phase 4: settle Solana invoices by REFERENCE. Returns (settled, unmatched).

        Structurally different from `_scan_onchain`, and deliberately so. The EVM
        pass sweeps every treasury transfer and then asks which invoice each
        amount belongs to — hence amount-jitter, to keep same-priced invoices
        apart. Here each PENDING invoice carries a unique Solana Pay reference
        key, so the question is inverted: ask the chain which transactions carry
        THIS invoice's marker. Matching is exact, and jitter would be strictly
        worse (Solana research mismatch #8).

        The idempotency primitives are shared, not re-invented: a signature is
        checked against `transaction_hash_already_settled` and the row is taken
        with the same `claim_for_settlement` CAS, so a signature settles at most
        one invoice EVER and a crash between claim and settle reverts.

        Fail-open at every step — a settlement watcher that raises stops
        watching.
        """
        if not solana_settle_enabled():
            return 0, 0
        from modules.x402 import invoicing, solana_settlement
        from core.wallet import solana_onchain
        from core.wallet.solana_x402 import usdc_mint

        try:
            wallet = self._agent_wallet() if hasattr(self, "_agent_wallet") else None
        except Exception:
            wallet = None
        try:
            from core.wallet.factory import get_agent_wallet
            wallet = wallet or get_agent_wallet()
            treasury = wallet.solana_address if wallet else ""
            mint = usdc_mint(getattr(wallet, "network", "testnet"))
        except Exception as exc:
            logger.debug("solana settle: no treasury/mint (%s)", exc)
            return 0, 0
        if not treasury:
            return 0, 0

        def _rpc(method, params):
            return solana_onchain._rpc(method, params)

        settled = unmatched = 0
        # Enumerated DIRECTLY, not through `list_payment_requests`: that helper
        # is tenant-scoped and requires a user_id, and a watcher has no single
        # tenant — passing one would silently skip every other. The first real
        # round trip failed here: the call raised TypeError for the missing
        # argument, the broad except below swallowed it, and the method returned
        # "settled=0 unmatched=0", which is exactly what a healthy idle run looks
        # like. The payment had landed and the invoice sat pending.
        #
        # A programming error is deliberately NOT caught here. A TypeError is not
        # an outage, and reporting one as "no work" is how a broken scan hides.
        database = await invoicing._resolve_db(self._db)
        pending = await database.fetch_all(
            """SELECT * FROM x402_payment_requests
                WHERE status = 'pending' AND chain = ?
                  AND json_extract(metadata, '$.chain_family') = 'svm'""",
            ("solana",))

        for row in (pending or []):
            row = dict(row)
            request_id = str(row.get("id") or "")
            if not request_id:
                continue
            reference = solana_settlement.reference_for_invoice(request_id)
            try:
                expected_raw = int(round(float(row.get("amount_usd") or 0) * 10 ** 6))
            except (TypeError, ValueError):
                continue
            claimed = False
            try:
                for signature in solana_settlement.scan_reference(reference, rpc=_rpc):
                    if await invoicing.transaction_hash_already_settled(
                            signature, db=self._db):
                        continue
                    tx = _rpc("getTransaction",
                              [signature, {"encoding": "jsonParsed",
                                           "maxSupportedTransactionVersion": 0}])
                    credited = solana_settlement.settlement_for(
                        tx, treasury=treasury, mint=mint, expected_raw=expected_raw)
                    if credited is None:
                        unmatched += 1
                        continue
                    if not await invoicing.claim_for_settlement(request_id, db=self._db):
                        break
                    claimed = True
                    ok = await invoicing.settle_payment_request(
                        request_id, transaction_hash=signature, db=self._db)
                    claimed = False
                    if ok:
                        settled += 1
                        logger.info("solana settle: invoice %s settled by %s",
                                    request_id, signature)
                    break
            except Exception as exc:
                logger.warning("solana settle: invoice %s failed (%s)", request_id, exc)
            finally:
                if claimed:
                    # Claimed but never settled — revert so the row is retryable
                    # rather than stranded in `settling`.
                    try:
                        await invoicing.revert_settlement_claim(request_id, db=self._db)
                    except Exception:
                        pass
        return settled, unmatched
    async def _settle_or_flag(self, transfers: list, treasury: str,
                               chain: str = "") -> tuple:
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
        even if this loop's ordering ever changes.

        `chain` (optional — empty for the direct/legacy call shape some tests
        use) resolves the known swap-router addresses. A transfer with NO
        matching invoice that arrives from one of them is correlated against
        our OWN recorded trades (`core.wallet.trade_index`, an exact
        broadcast-hash match): a hit is our trade proceeds and is skipped with
        a `payment_self_proceeds` breadcrumb; a MISS is treated as a real
        unmatched payment. The routers are shared public infrastructure, so the
        address alone can never be the deciding evidence."""
        from modules.x402 import invoicing

        known_routers = _known_swap_router_addresses(chain)
        # Loaded at most once per batch, and only when a router-sourced
        # transfer actually needs it (the ledger is a whole-file read).
        own_trade_refs: Optional[set] = None
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
                    # A router-sourced transfer is skipped ONLY when it
                    # correlates to a trade we ourselves broadcast. The address
                    # alone proves nothing — a Uniswap router is shared public
                    # infrastructure, so an address-only skip also swallowed a
                    # genuine payer who routed through it (and every overpayment
                    # that missed the exact-amount match) with no settlement, no
                    # notice and no telemetry.
                    if (transfer.get("from") or "").lower() in known_routers:
                        if own_trade_refs is None:
                            from core.wallet import trade_index
                            own_trade_refs = trade_index.own_trade_tx_refs()
                        if str(tx_hash or "").strip().lower() in own_trade_refs:
                            self._note_self_proceeds(transfer, treasury)
                            continue
                        logger.warning(
                            "settlement watcher: transfer %s came from swap "
                            "router/aggregator %s but matches NO trade in our "
                            "own ledger — treating it as a real unmatched "
                            "payment, not as our proceeds",
                            transfer.get("tx_hash"), transfer.get("from"))
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
    def _note_self_proceeds(self, transfer: dict, treasury: str) -> None:
        """A router-sourced transfer confirmed as OUR OWN trade proceeds.

        It is correctly not an owner notice — the agent moved this money on
        purpose — but it must never be INVISIBLE either: a skip with no record
        is exactly how a mis-correlated real payment would vanish without
        trace. One low-severity durable breadcrumb, no owner ping.
        """
        from modules.x402.invoicing import _emit

        owner = ""
        try:
            from core.instance import resolve_owner_principal
            owner = resolve_owner_principal() or ""
        except Exception:
            owner = ""
        try:
            _emit("payment_self_proceeds", user_id=owner, session_id="", attrs={
                "tx_hash": transfer.get("tx_hash"),
                "from": transfer.get("from"),
                "amount_usd": transfer.get("amount_usd"),
                "block": transfer.get("block"),
                "treasury": treasury,
                "correlation": "own_trade_tx_hash",
            })
        except Exception:
            logger.debug("settlement watcher: self-proceeds breadcrumb failed "
                         "(fail-open)", exc_info=True)
        logger.info(
            "settlement watcher: transfer %s from swap router/aggregator %s "
            "correlates to our OWN recorded trade — skipping the "
            "unmatched-payment owner notice",
            transfer.get("tx_hash"), transfer.get("from"))

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
        # A telemetry row the owner has to go read is not a notice. This is an
        # unexpected on-chain payment the owner must reconcile — deliver it over
        # the durable rail on the critical lane (money-actionable, low-frequency).
        amt = transfer.get("amount_usd")
        amt_str = f"${amt}" if amt is not None else "an unknown amount"
        await self._push_owner_notice(owner, (
            f"Unmatched on-chain payment: {amt_str} from {transfer.get('from')} "
            f"landed in the treasury (tx {transfer.get('tx_hash')}) but matched NO "
            f"pending invoice. Nothing was auto-settled; please reconcile."),
            source="payment_unmatched")
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
