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
    """``(rpc_url, expected_chain_id)`` for a scannable EVM chain, or ``None``.

    ⚠️ ASSET-FREE since 046. It used to answer for ``base``/``base-sepolia``
    ONLY, with a hardcoded USDC address baked into the return — so detection on
    every other chain was a one-warning no-op, forever. The token address now
    comes from the PENDING INVOICE's own asset row, and the chain's RPC and
    chain id come from `core/wallet/chains.py`, which already pins both.

    Precedence for the URL is unchanged: ``X402_SETTLEMENT_RPC`` (any chain) >
    the chain's pinned RPC > the chain row's public fallback.

    ⚠️ ``base-sepolia`` keeps its OWN branch. The money chain registry
    deliberately carries only chains verified for TRADING, so it has no testnet
    row; the x402 module has always owned the sepolia RPC and chain id and
    continues to.
    """
    import os
    from modules.x402.artifact import _BASE_SEPOLIA_CHAIN_ID, _is_sepolia_chain
    override = os.getenv("X402_SETTLEMENT_RPC", "").strip()
    chain = (chain or "").strip().lower()
    if _is_sepolia_chain(chain):
        return (override or _SEPOLIA_RPC_DEFAULT, _BASE_SEPOLIA_CHAIN_ID)
    from core.wallet import chains as _chains
    row = _chains.get(chain)
    if row is None or row.family != "evm" or not row.chain_id:
        return None
    from core.wallet.onchain import rpc_url_for_chain
    url = override or rpc_url_for_chain(row.name) or row.public_rpc
    if not url:
        return None
    return (url, int(row.chain_id))


def scan_key(treasury: str, chain: str, asset_address) -> str:
    """The `settlement_scan` checkpoint key for one ``(treasury, chain, asset)``.

    ⚠️ The DEFAULT asset keeps the BARE treasury key. That table is keyed on a
    single ``treasury`` column, and a key change re-seeds the checkpoint near
    head on the next tick — which scans NOTHING and silently skips every
    transfer in the gap. Continuity for the live USDC rail is worth the special
    case.
    """
    from core.payments.assets import DEFAULT_ASSET_ID, resolve
    default = resolve(DEFAULT_ASSET_ID)
    addr = (asset_address or "").strip().lower()
    if (default is not None and (chain or "").strip().lower() == default.chain
            and addr == (default.address or "").lower()):
        return treasury
    return f"{treasury}|{(chain or '').strip().lower()}|{addr}"


async def pending_scan_groups(treasury: str, *, db=None):
    """``[(chain, asset_address, decimals, asset_id), ...]`` — one row per
    DISTINCT asset that has a PENDING invoice at this treasury.

    Scanning is driven by what is actually OWED, not by a configured list: an
    asset nobody is waiting on costs no ``eth_getLogs`` call, and a newly pinned
    asset needs no scanner configuration at all.

    A legacy row (NULL asset columns) contributes the DEFAULT asset's group, so
    an invoice outstanding across the deploy keeps being scanned. A non-EVM row
    contributes nothing — its settlement is the reference pass, which asks a
    different question of a different chain.
    """
    from core.payments.assets import DEFAULT_ASSET_ID, resolve
    from modules.x402 import invoicing
    database = await invoicing._resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        """SELECT DISTINCT chain, asset_address, asset_decimals, asset_id
           FROM x402_payment_requests
           WHERE status = 'pending' AND recipient = ?""",
        (invoicing.normalize_recipient(treasury),)) or []
    default = resolve(DEFAULT_ASSET_ID)
    out = []
    for r in rows:
        chain = str(r["chain"] or "").strip().lower()
        addr = (r["asset_address"] or "").strip().lower()
        if not addr:
            if default is None:
                continue
            group = (default.chain, (default.address or "").lower(),
                     default.decimals, default.asset_id)
        else:
            if _resolve_scan_target(chain) is None:
                continue          # non-EVM, or a chain we cannot reach
            group = (chain, addr, int(r["asset_decimals"] or 6),
                     str(r["asset_id"] or DEFAULT_ASSET_ID))
        if group not in out:
            out.append(group)
    return out


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
        """Scan the treasury for new transfers of every OWED asset and
        auto-settle any exact-amount match. Returns (settled, unmatched).

        ⚠️ ONE ``eth_getLogs`` per ``(chain, asset)`` that actually has a pending
        invoice (046). Before this the pass scanned exactly one contract on
        exactly one chain, so an invoice in any other asset could never settle
        however correctly it was minted.

        Always (0, 0) when detection is off, no treasury is configured, nothing
        is owed, or the RPC is unreachable — fail-open at every step; nothing
        here ever raises past this method, and the caller wraps it again
        defensively.
        """
        from modules.x402 import invoicing
        if not invoicing.x402_settle_onchain_detect_enabled():
            return 0, 0

        from modules.x402.x402_integration import get_x402_config
        cfg = get_x402_config()
        treasury = (cfg.get("pay_to") or "").strip()
        if not treasury:
            return 0, 0
        treasury_key = treasury.lower()

        # ⚠️ The CONFIGURED chain's own asset is scanned EVERY tick, owed or
        # not. Scanning only what is owed would be cheaper and would silence
        # `payment_unmatched` — the event that tells an owner money arrived
        # which nothing accounted for. "No unmatched payments" must never come
        # to mean "I was not looking". Every OTHER asset is owed-driven: an
        # asset nobody is waiting on costs no eth_getLogs call.
        groups = await pending_scan_groups(treasury_key, db=self._db)
        default_group = await self._default_scan_group(cfg)
        if default_group is not None and default_group not in groups:
            groups = [default_group] + groups
        if not groups:
            # The configured chain is unscannable AND nothing is owed elsewhere.
            return 0, 0

        settled = unmatched = 0
        for chain, asset_address, decimals, asset_id in groups:
            try:
                s, u = await self._scan_one_asset(
                    treasury, treasury_key, chain, asset_address, decimals,
                    asset_id)
                settled += s
                unmatched += u
            except Exception:
                # One asset's RPC failing must never stop the others. Its own
                # checkpoint is untouched, so the range retries next tick.
                logger.warning(
                    "x402 settlement scan: asset %s on %s failed this tick "
                    "(its checkpoint is held; other assets continue)",
                    asset_id, chain, exc_info=True)
        return settled, unmatched


    async def _default_scan_group(self, cfg):
        """The ``(chain, asset_address, decimals, asset_id)`` of the configured
        chain's own payable asset, or ``None`` when that chain is not scannable.

        This is the group whose checkpoint must exist BEFORE the first invoice,
        not after it.
        """
        chain = (cfg.get("network") or "").strip().lower()
        if not chain or _resolve_scan_target(chain) is None:
            return None
        try:
            from modules.x402.invoicing import resolve_invoice_asset
            asset = resolve_invoice_asset(chain, None)
        except Exception:
            return None
        return (chain, (asset.address or "").lower(), asset.decimals,
                asset.asset_id)

    async def _seed_only(self, treasury, treasury_key, chain, asset_address,
                         decimals, asset_id) -> None:
        """Create this asset's checkpoint near head if it has none. No scan."""
        from modules.x402 import invoicing, onchain_probe

        token_addr = self._usdc_addr or asset_address
        key = scan_key(treasury_key, chain, token_addr)
        if await invoicing.get_scan_checkpoint(key, db=self._db) is not None:
            return
        target = _resolve_scan_target(chain)
        if target is None:
            return
        rpc_url, expected_chain_id = target
        call = self._rpc_call
        if call is None:
            from core.wallet.onchain import _rpc

            def call(method, params, _url=rpc_url):
                return _rpc(_url, method, params)
        if not await self._verify_scan_network(call, expected_chain_id, rpc_url,
                                               chain):
            return
        head = await asyncio.to_thread(onchain_probe.get_head_block, call)
        if head is None:
            return
        await invoicing.advance_scan_checkpoint(
            key, max(0, head - _scan_confirmations()), db=self._db)

    async def _scan_one_asset(self, treasury, treasury_key, chain,
                              asset_address, decimals, asset_id) -> tuple:
        """One ``(chain, asset)`` sweep, with its OWN checkpoint.

        Every pre-046 guard is preserved inside this loop body: the chain-id
        verification, the never-sweep-from-genesis first-run seed, the
        confirmations buffer, the per-tick span cap, and — the one that matters
        most — HOLDING the cursor when the probe returns ``None`` (the range was
        not scanned), because advancing past an unread range burns it forever.
        """
        from modules.x402 import invoicing, onchain_probe

        target = _resolve_scan_target(chain)
        if target is None:
            # Something is OWED in an asset on a chain we cannot reach. Never a
            # silent no-op (the pre-W1.2 sepolia trap): say so once per chain.
            warned = getattr(self, "_scan_target_warned", None)
            if warned is None:
                warned = self._scan_target_warned = set()
            if chain not in warned:
                warned.add(chain)
                logger.warning(
                    "x402 on-chain detection: %s invoice(s) are pending in "
                    "asset %s on chain %r, which is not scannable — those "
                    "invoices cannot settle on-chain", asset_id, asset_id, chain)
            return 0, 0
        rpc_url, expected_chain_id = target

        logged = getattr(self, "_scan_target_logged", None)
        if logged is None:
            logged = self._scan_target_logged = set()
        if (chain, asset_id) not in logged:
            logged.add((chain, asset_id))
            logger.info("x402 on-chain detection active: chain=%s asset=%s rpc=%s",
                        chain, asset_id, _redact_rpc(rpc_url))

        call = self._rpc_call
        # `_usdc_addr` is the pre-046 test seam: an explicit override still wins,
        # for the one asset a legacy test pins.
        token_addr = self._usdc_addr or asset_address
        if call is None:
            from core.wallet.onchain import _rpc

            def call(method, params, _url=rpc_url):
                return _rpc(_url, method, params)

        if not await self._verify_scan_network(call, expected_chain_id, rpc_url,
                                               chain):
            return 0, 0

        # M9: the probe does synchronous urllib I/O. Running it inline froze the
        # whole agent/API event loop every tick — offload to a thread.
        head = await asyncio.to_thread(onchain_probe.get_head_block, call)
        if head is None:
            return 0, 0

        key = scan_key(treasury_key, chain, token_addr)
        safe_head = head - _scan_confirmations()
        last = await invoicing.get_scan_checkpoint(key, db=self._db)
        if last is None:
            # First run for THIS asset: seed near the head and scan NOTHING this
            # tick — never sweep from genesis.
            #
            # ⚠️ A payment that landed BEFORE this seed is not enumerated. For
            # the configured chain's own asset that window does not exist (it is
            # seeded every tick, owed or not). For a NEWLY pinned asset it does,
            # so say it out loud rather than let an operator discover it by
            # losing a payment.
            logger.warning(
                "x402 on-chain detection: first sight of asset %s on %s — "
                "seeding the checkpoint at block %s and scanning nothing this "
                "tick. A transfer of this token that ALREADY landed will not be "
                "detected; pin an asset before inviting payment in it.",
                asset_id, chain, max(0, safe_head))
            await invoicing.advance_scan_checkpoint(key, max(0, safe_head),
                                                    db=self._db)
            return 0, 0

        from_block = last + 1
        if from_block > safe_head:
            return 0, 0  # nothing new past the confirmations buffer yet
        to_block = min(safe_head, from_block + _scan_max_span() - 1)

        transfers = await asyncio.to_thread(
            onchain_probe.scan_treasury_transfers,
            call, token_addr, treasury, from_block, to_block, decimals=decimals)
        if transfers is None:
            # The range was NOT scanned (RPC error). Hold the cursor so the next
            # tick retries it — advancing here would burn the range forever,
            # since advance_scan_checkpoint refuses to regress, and a payer's
            # real transfer inside it would never be enumerated (audit
            # 2026-08-07 #1, Critical).
            logger.warning(
                "x402 settlement scan: %s blocks %s..%s UNSCANNED (rpc failure) "
                "— checkpoint held at %s, will retry next tick",
                asset_id, from_block, to_block, last)
            return 0, 0
        settled, unmatched = await self._settle_or_flag(
            transfers, treasury_key, chain, asset_address=token_addr,
            decimals=decimals)
        # Advance for the fully-processed range regardless of match outcome — an
        # unmatched transfer is recorded via payment_unmatched, not by holding
        # the cursor back.
        await invoicing.advance_scan_checkpoint(key, to_block, db=self._db)
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
                               chain: str = "", *, asset_address=None,
                               decimals: int = 6) -> tuple:
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
                # 046: ASSET-KEYED integer match. Matching a float amount_usd
                # treasury-wide would let a transfer of one token settle an
                # invoice denominated in another.
                #
                # The pre-046 DIRECT-call shape (a transfer dict carrying only
                # `amount_usd`, and no asset) is still supported — several tests
                # and any out-of-tree caller use it — by routing through the
                # back-compat shim, which resolves the default asset.
                raw = transfer.get("amount_raw")
                if raw is None or asset_address is None:
                    match = await invoicing.match_pending_invoice_by_amount(
                        transfer.get("amount_usd"), treasury, chain=chain or None,
                        db=self._db)
                else:
                    match = await invoicing.match_pending_invoice(
                        treasury, asset_address, raw,
                        chain=chain, decimals=decimals,
                        # 046: every PAYABLE kind. Without `room_action` here a
                        # paid offer sits pending forever with the money
                        # already received.
                        kinds=tuple(invoicing.PAYABLE_KINDS), db=self._db)
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
            from core.instance import resolve_owner_user_id
            owner = resolve_owner_user_id()
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
            from core.instance import resolve_owner_user_id
            owner = resolve_owner_user_id()
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
            f"pending invoice. Nothing was auto-settled; please reconcile."
            + self._open_room_offer_hint()),
            source="payment_unmatched")

    def _open_room_offer_hint(self) -> str:
        """Name the rooms with an open paid-action offer, when there are any.

        ⚠️ Deliberately a HINT to the owner and NOT a message posted into a
        room. An on-chain transfer carries no room identity, so "this payment
        was probably yours" would be a guess published to strangers — but an
        unmatched payment while an offer is open is almost always a member who
        sent the wrong amount, and the owner cannot act on a notice that does
        not say so. Fail-open: an unreadable store adds nothing.
        """
        try:
            import os as _os

            from core.runtime_paths import data_dir_or_home
            from core.surfaces.room_action_store import OfferStore, store_path
            container = (getattr(self, "_room_container", None)
                         or getattr(getattr(self, "task_agent", None),
                                    "container", None))
            cfg = getattr(container, "config", None) if container else None
            home = data_dir_or_home(getattr(cfg, "data_dir", None))
            path = store_path(home)
            if not _os.path.exists(path):
                return ""
            rooms = OfferStore(path).pending_rooms()
            if not rooms:
                return ""
            named = ", ".join(f"{s}:{c}" for s, c in rooms[:3])
            return (f" There is an open paid-action offer in {named} — if the "
                    f"amount is close, a member likely sent the wrong one.")
        except Exception:
            return ""
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
