"""Agent-initiated x402 payment requests — the invoice side of the money loop.

Until now only the HTTP middleware could produce `x402_payment_requests` rows,
and only for already-settled platform charges. This module lets the AGENT create
a *pending* payment request (an invoice) from inside a session: amount, purpose,
an optional free-form ``payer_contact`` (the payer's own contact info, shown on
the invoice — no contact book, no schema change), expiry — riding the existing
table, treasury config (`X402_PAYMENT_RECIPIENT` / `X402_DEFAULT_CHAIN`) and
telemetry event log.

Invoice rows are distinguished by ``metadata.kind == "agent_invoice"`` and carry
the originating ``session_id`` so the settlement watcher
(`modules/x402/settlement_watcher.py`) can re-enter that session when the
invoice settles. Settlement itself is an explicit, attested transition
(``settle_payment_request`` — owner CLI / API / a future payable endpoint), never
inferred; expiry is enforced by the watcher via the row's ``deadline``.

Rails: amounts are bounded by ``X402_INVOICE_MAX_USD`` (an
absurd invoice is a reputation incident) and creation is capped per tenant per
day (``X402_INVOICE_DAILY_MAX``). Deliberately NOT recorded into the wallet
PolicyGate spend audit — receivables in the spend window would corrupt the 24h
rolling spend caps. Every creation/settlement/expiry emits a first-class
telemetry event (``payment_requested`` / ``payment_settled`` / ``payment_expired``).
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 046: defined in `invoice_assets` and imported here, not the reverse —
#: that module is imported BY this one for the re-exports below.
from modules.x402.invoice_assets import INVOICE_KIND  # noqa: E402


def _norm_tx(tx_hash) -> Optional[str]:
    """Canonical form for a transaction hash used as a replay key.

    M1 (audit 2026-08-22): eth_getLogs returns lowercase; a facilitator returns
    whatever it likes. A raw string compare let the SAME settlement transfer be
    re-detected on-chain and settle a SECOND same-amount invoice. Normalize at
    EVERY store and compare site — one side is not enough.

    Ethereum tx hashes are hex, so lowercasing them is safe and lossless. A
    SOLANA transaction signature is base58 — CASE-SIGNIFICANT — so folding it
    destroys provenance: the stored value resolves on no explorer and matches
    no RPC ``getTransaction`` (the same landmine ``normalize_recipient`` closes
    for addresses, one column over). The shape is unambiguous: an EVM hash is
    ``0x`` + hex, a Solana signature never starts with ``0x`` — so fold ONLY
    the ``0x`` form and keep everything else verbatim. Replay-guard semantics
    are unchanged either way (store and compare fold identically).
    """
    if not tx_hash:
        return None
    tx = str(tx_hash).strip()
    return tx.lower() if tx.startswith(("0x", "0X")) else tx


def invoice_max_usd() -> float:
    try:
        return float(os.getenv("X402_INVOICE_MAX_USD", "50"))
    except ValueError:
        return 50.0


def invoice_daily_max() -> int:
    try:
        return int(os.getenv("X402_INVOICE_DAILY_MAX", "10"))
    except ValueError:
        return 10


def x402_invoicing_enabled() -> bool:
    """Gate for the whole agent-invoicing surface: the `x402_invoice` tool
    (tools/x402/__init__.py delegates here — this is the shared SSOT so the two
    never disagree), the public settlement/pay endpoints (api/x402_endpoints.py),
    and the autonomy-runtime settlement watcher startup gate. Default OFF; ON
    under effective AUTONOMY_MODE=autonomous via _mode_capability_default (013 T2
    review fix, Finding 2 — was raw-env-only, leaving invoices creatable but
    unpayable/unsettleable under autonomous mode). Explicit X402_INVOICE_ENABLED
    always wins. Lazy + guarded import, fail-closed to OFF on any fault.

    core/autonomy_runtime.py does NOT import this (a core-tier module importing
    modules.x402 would put a server-tier module on the core import graph — the
    C3 boundary, see tests/test_core_server_boundary.py) and instead applies the
    same guarded-OR locally."""
    from core.env import bool_env
    try:
        from core.config_policy import _mode_capability_default
        default = _mode_capability_default("X402_INVOICE_ENABLED")
    except Exception:
        default = False
    return bool_env("X402_INVOICE_ENABLED", default)


def x402_settle_onchain_detect_enabled() -> bool:
    """Task 11 (Phase 2): whether the settlement watcher additionally scans
    the treasury address for plain USDC transfers (no facilitator) and
    auto-settles the matching pending invoice. Default OFF; ON under effective
    AUTONOMY_MODE=autonomous via _mode_capability_default (W1.5, 2026-08-21 —
    receive-side only, same lane as X402_INVOICE_ENABLED; explicit env always
    wins). The watcher ALSO requires a scannable chain + treasury before it
    actually scans (see `settlement_watcher.py::_resolve_scan_target`); this
    getter is the single flag-parse SSOT shared by that gate and the
    amount-jitter gate below."""
    from core.env import bool_env
    try:
        from core.config_policy import _mode_capability_default
        default = _mode_capability_default("X402_SETTLE_ONCHAIN_DETECT")
    except Exception:
        default = False
    return bool_env("X402_SETTLE_ONCHAIN_DETECT", default)


def x402_invoice_amount_jitter_enabled() -> bool:
    """Whether `create_payment_request` nudges a colliding amount (Task 11).
    Default ON, but INERT unless on-chain detection is also enabled
    (`x402_settle_onchain_detect_enabled`) — the jitter exists solely to keep
    on-chain amount-matching unambiguous; with detection off it would just be
    a pointless amount perturbation. See `_dedupe_amount_for_treasury`.

    I2 safety fix (Task 11 review): this flag can no longer DISABLE jitter
    while detection is ON — ``X402_SETTLE_ONCHAIN_DETECT=true`` +
    ``X402_INVOICE_AMOUNT_JITTER=false`` is an unsafe combination (auto-
    settlement from on-chain transfers with ZERO disambiguation), so
    `create_payment_request` forces jitter on internally in that case and logs
    a one-time-per-call notice. Setting this false only has effect (staying
    truly inert) while detection is also off — see `_jitter_should_apply`."""
    from core.env import bool_env
    return bool_env("X402_INVOICE_AMOUNT_JITTER", True)


def _jitter_should_apply(chain: Optional[str] = None) -> bool:
    """The ACTUAL jitter gate `create_payment_request` uses (I2 fix): jitter
    is forced ON whenever on-chain detection is on, regardless of the
    ``X402_INVOICE_AMOUNT_JITTER`` value — logging a notice when the flag was
    explicitly set to disable it. When detection is off, jitter stays fully
    inert (byte-identical legacy amounts) exactly as before."""
    # Solana carries a per-invoice REFERENCE key, which is an exact correlator.
    # Jitter exists solely to disambiguate amount-matching on EVM, so applying it
    # here would perturb the amount for no benefit at all.
    if chain and _chain_family(chain) == "svm":
        return False
    detect_on = x402_settle_onchain_detect_enabled()
    if not detect_on:
        return False
    if not x402_invoice_amount_jitter_enabled():
        logger.warning(
            "X402_SETTLE_ONCHAIN_DETECT is on but X402_INVOICE_AMOUNT_JITTER "
            "is explicitly off — forcing amount-collision jitter ON anyway: "
            "on-chain auto-settlement with no jitter cannot disambiguate a "
            "same-amount pending-invoice collision (Task 11 I2 safety fix)."
        )
    return True
from modules.x402._db import resolve_db as _resolve_db  # shared plumbing (one home)
from modules.x402.invoice_sizing import _UNSET, _size_invoice_raw, scale_raw  # 046 §4.4


def _emit(kind: str, *, user_id: str, session_id: str = "", attrs: Optional[dict] = None) -> None:
    """First-class money telemetry — thin label-binding over the shared
    ``modules.x402._db.emit`` (the body was duplicated in subscriptions.py)."""
    from modules.x402._db import emit
    emit(kind, source="x402_invoice", user_id=user_id, session_id=session_id, attrs=attrs)


def _row_metadata(row: Dict[str, Any]) -> dict:
    """metadata column, tolerant of both shapes: DatabaseConnection.fetch_* auto-
    parses JSON-looking TEXT into a dict; raw sqlite rows give the str."""
    meta = row.get("metadata")
    if isinstance(meta, dict):
        return meta
    try:
        return json.loads(meta or "{}")
    except Exception:
        return {}


#: Kinds that are AGENT-CREATED payable rows, as opposed to the middleware's
#: already-settled platform-charge rows. Every one of these is readable through
#: `get_payment_request`, servable as a public challenge, and settleable.
#:
#: ⚠️ A new producer MUST be added here. `is_invoice_row` is the filter on the
#: public read path, so a kind missing from this set produces rows that mint
#: fine, take real money, and then read as NOT FOUND — no challenge, no
#: settlement, no way for anyone to see what happened.
PAYABLE_KINDS = (INVOICE_KIND, "room_action")


def is_invoice_row(row: Dict[str, Any]) -> bool:
    """Is this an agent-created payable row (not a platform-charge row)?"""
    return _row_metadata(row).get("kind") in PAYABLE_KINDS


def normalize_recipient(address: str, chain: Optional[str] = None) -> str:
    """Store/lookup form for a recipient address.

    Lowercasing is an EVM-hex CONVENIENCE — hex is case-insensitive, so folding
    it makes comparisons total. base58 is not: folding a Solana address yields
    different bytes, or nothing decodable at all, so the stored value would be
    an address nobody holds and every lookup would miss.

    This is THE function for that decision. Every site that writes a recipient
    or matches on one calls it, because a store that folds and a lookup that
    does not (or the reverse) can never match — which is how an svm invoice
    would sit pending forever while its payment sat on-chain.
    """
    address = (address or "").strip()
    if chain and _chain_family(chain) == "svm":
        return address
    return address.lower()


def _svm_treasury() -> Optional[str]:
    """The agent's Solana receiving address, or "" when unavailable."""
    try:
        from core.wallet.factory import get_agent_wallet
        wallet = get_agent_wallet()
        return wallet.solana_address if wallet else None
    except Exception:
        return None


def _chain_family(chain: str) -> str:
    """``"evm"`` or ``"svm"`` for an invoice's chain, from the registry.

    Fails SAFE rather than open: an unrecognised chain keeps the long-standing
    EVM behaviour instead of silently entering a Solana path that cannot serve
    it. Guessing from the chain NAME would be the same mistake the address
    validator refuses to make.
    """
    try:
        from core.wallet import chains
        row = chains.get(chain)
        return row.family if row is not None else "evm"
    except Exception:
        return "evm"


def _sanitize_correspondent_ref(ref: Optional[Dict[str, Any]]) -> Optional[dict]:
    """Keep only the correspondent registry key fields (surface/address/thread_id),
    stringified and bounded, so a settled invoice can be delivered as DATA on the
    correspondent rail rather than as an owner self-wake. None when absent/invalid."""
    if not isinstance(ref, dict):
        return None
    surface = str(ref.get("surface") or "").strip()[:64]
    address = str(ref.get("address") or "").strip()[:256]
    if not surface or not address:
        return None
    return {"surface": surface, "address": address,
            "thread_id": str(ref.get("thread_id") or "").strip()[:128]}


async def create_payment_request(
    *,
    user_id: str,
    session_id: str,
    amount_usd: float,
    purpose: str,
    payer_contact: Optional[str] = None,
    payer_hint: Optional[str] = None,
    expiry_hours: float = 72.0,
    correspondent_ref: Optional[Dict[str, Any]] = None,
    subscription_id: Optional[str] = None,
    chain: Optional[str] = None,
    asset_id: Optional[str] = None,
    amount_raw: Optional[int] = None,
    quoter: Any = _UNSET,
    kind: str = INVOICE_KIND,
    extra_metadata: Optional[Dict[str, Any]] = None,
    db=None,
) -> Dict[str, Any]:
    """Create a pending invoice row. Returns payment instructions, or raises
    ValueError with an agent-readable reason (caps, config, validation).

    ``payer_contact`` is a free-form "billed to" string (name/email/handle),
    stored and surfaced verbatim — no contact book, no schema change.
    ``payer_hint`` is a deprecated alias kept for back-compat; when both are
    given ``payer_contact`` wins.

    ``subscription_id`` (Task 14): when this invoice is a watchtower
    subscription's renewal, the id rides in ``metadata.subscription_id`` so
    the settlement watcher can detect it on settlement and call
    ``modules.x402.subscriptions.apply_settlement`` — extending the
    subscription's ``paid_through`` instead of treating it as an ordinary
    one-off invoice. None for every other invoice (unchanged legacy shape).

    046 Phase 0. ``asset_id`` names WHICH token this invoice is payable in (see
    `core/payments/assets.py`); unset resolves the chain's canonical USDC, which
    is what every pre-046 caller meant. ``amount_raw`` is the exact integer the
    settlement scan matches on — pass it for a non-stable asset, whose sizing is
    the quoter's job (proposal 046 §4.4), and leave it unset for a dollar-pegged
    one. ``kind``/``extra_metadata`` let a NON-agent-invoice producer ride this
    ONE mint path rather than forking it; a distinct ``kind`` also keeps that
    producer out of the agent's own ``X402_INVOICE_DAILY_MAX`` bucket."""
    # H5: the owner kill-switch halts ALL autonomous activity — including minting new
    # payment requests (agent invoices AND the settlement watcher's auto-mode renewals),
    # not just outbound spend. Fail closed: a probe error blocks creation.
    try:
        from core.config_policy import AutonomyConfig
        halted = AutonomyConfig.autonomy_halted()
    except Exception as e:
        raise ValueError(f"invoicing refused: kill-switch probe failed ({e}) — failing closed")
    if halted:
        raise ValueError("invoicing refused: autonomy is HALTED (owner kill-switch)")
    if not user_id:
        # An empty tenant would create a SHARED anonymous invoice bucket (cross-
        # tenant reads + a shared daily cap) — refuse, mirroring MEMORY_REQUIRE_USER_ID.
        raise ValueError("invoicing requires an authenticated tenant (empty user_id refused)")
    if not purpose or not purpose.strip():
        raise ValueError("purpose is required — the payer must know what they are paying for")
    amount_usd = float(amount_usd)
    if amount_usd <= 0:
        raise ValueError("amount_usd must be positive")
    cap = invoice_max_usd()
    if amount_usd > cap:
        raise ValueError(
            f"amount ${amount_usd:.2f} exceeds the invoice ceiling ${cap:.2f} "
            "(X402_INVOICE_MAX_USD)"
        )
    from modules.x402.x402_integration import get_x402_config
    cfg = get_x402_config()
    # The chain is a property of THIS invoice. It used to come only from global
    # config, so there was no way to request a Solana invoice at all — every row
    # came out evm-family with no reference and `_scan_solana` skipped it, which
    # made Phase 4's wiring unreachable in practice. The env workaround
    # (X402_DEFAULT_CHAIN=solana) is process-wide and would silently retarget any
    # EVM invoice created alongside it, so it is deliberately not the fix.
    chain = (chain or cfg.get("network") or "base").strip().lower()
    family = _chain_family(chain)
    if family == "svm":
        # A DIFFERENT key entirely — the EVM `pay_to` is not an address anyone
        # holds on Solana, and paying it there strands the funds permanently.
        recipient = _svm_treasury()
        if not recipient:
            raise ValueError(
                f"cannot invoice on {chain}: the agent wallet has no Solana "
                f"address to receive at (needs AGENT_WALLET_ENABLED and a "
                f"BIP-39 master seed)")
    else:
        recipient = (cfg.get("pay_to") or "").strip()
    if not recipient:
        raise ValueError("no treasury configured — set X402_PAYMENT_RECIPIENT")

    # 046 Phase 0: WHICH token, at what precision, in what raw amount. Resolved
    # BEFORE the daily-cap query so an unknown asset is refused without ever
    # touching the store.
    asset = resolve_invoice_asset(chain, asset_id)
    raw = _size_invoice_raw(amount_usd, asset, amount_raw, quoter)
    # The jitter below moves the dollars AFTER this one sizing decision, so
    # every later raw figure scales it — never re-quotes. See `invoice_sizing`.
    _base_raw, _base_usd = raw, float(amount_usd)
    def _raw_for(usd: float) -> int:
        return scale_raw(_base_raw, _base_usd, usd)
    if raw <= 0:
        raise ValueError(
            f"invoice amount ${amount_usd} resolves to {raw} raw units of "
            f"{asset.symbol or asset.asset_id} — refusing rather than minting an "
            f"invoice nobody can pay")
    if asset.min_amount_raw and raw < asset.min_amount_raw:
        raise ValueError(
            f"{raw} raw units is below the {asset.asset_id} floor of "
            f"{asset.min_amount_raw} (set with `polyrob wallet asset add "
            f"--min-amount`)")

    database = await _resolve_db(db)
    if database is None:
        raise ValueError("payment-request store unavailable (no database service)")

    # 046: the daily cap bounds the AGENT's own judgment-driven invoicing. A
    # non-agent producer (a room action) rides this same mint path but is
    # bounded by its OWN per-payer and per-target caps, so charging it against
    # this bucket would let one busy room exhaust the agent's ability to invoice
    # at all. Kind-scoped, and skipped entirely for a non-agent kind.
    daily_cap = invoice_daily_max() if kind == INVOICE_KIND else 0
    # Tenant match covers both storage shapes: user_id column when the tenant has
    # a user_profiles row, metadata.tenant_id when the FK fallback stored NULL.
    # json_extract (SQLite JSON1, bundled) not LIKE: a LIKE pattern treats `_`/`%`
    # as wildcards, and real tenant ids contain underscores (u_<hex>), so
    # 'u_abc' would also match a lookalike 'uXabc' row (G-14). The kind filter is
    # ALSO json_extract (L8): the boot-time subscription dedup rewrites metadata
    # via json_set -> compact JSON ("kind":"agent_invoice", no space), which the
    # old spaced LIKE '%"kind": "agent_invoice"%' silently stops matching.
    row = await database.fetch_one(
        """SELECT COUNT(*) AS n FROM x402_payment_requests
           WHERE (user_id = ? OR json_extract(metadata, '$.tenant_id') = ?)
             AND created_at >= datetime('now', '-1 day')
             AND json_extract(metadata, '$.kind') = ?""",
        (user_id, user_id, kind),
    )
    if daily_cap and row and int(row.get("n") or 0) >= daily_cap:
        raise ValueError(
            f"daily invoicing cap reached ({daily_cap}/day, X402_INVOICE_DAILY_MAX)"
        )

    request_id = f"inv_{uuid.uuid4().hex[:12]}"
    nonce = f"inv_{uuid.uuid4().hex}"
    expiry_hours = max(0.1, float(expiry_hours))
    deadline = int(time.time() + expiry_hours * 3600)
    contact = (payer_contact or payer_hint or "").strip()[:200] or None
    # user_id has an FK to user_profiles; an agent tenant (e.g. "rob") may not
    # exist there. Pre-check and store NULL in the column when absent — the
    # tenant stays queryable via metadata.tenant_id (every reader matches both).
    column_user: Optional[str] = user_id
    try:
        profile = await database.fetch_one(
            "SELECT 1 AS ok FROM user_profiles WHERE user_id = ?", (user_id,))
        if not profile:
            column_user = None
    except Exception:
        column_user = None

    async def _insert(final_amount: float) -> None:
        # Phase 4: which watcher pass owns this row, and — for Solana — the
        # payer-facing marker. Stamped at CREATION because the family is a
        # property of the invoice, not something a scanner should infer later.
        solana_reference = None
        if family == "svm":
            from modules.x402.solana_settlement import reference_for_invoice
            solana_reference = reference_for_invoice(request_id)
        metadata_dict = {
            "kind": kind,
            "chain_family": family,
            "solana_reference": solana_reference,
            "session_id": session_id,
            "tenant_id": user_id,
            "purpose": purpose.strip()[:500],
            "payer_contact": contact,
            "wake_delivered": False,
            "correspondent_ref": _sanitize_correspondent_ref(correspondent_ref),
            "subscription_id": subscription_id,
        }
        if extra_metadata:
            metadata_dict.update(extra_metadata)
        metadata = json.dumps(metadata_dict)
        # 046: the jitter path moves `final_amount` AFTER the sizing above. Re-
        # derive the raw amount from the FINAL figure, or the row's amount_raw
        # describes the pre-jitter price and the on-chain match never fires.
        # ⚠️ Reads the ENCLOSING `amount_raw`, which the pinned-raw dedupe above
        # may have nudged. Capturing the original would write a row whose
        # payable integer is one nobody was quoted.
        final_raw = (int(amount_raw) if amount_raw is not None
                     else _raw_for(final_amount))
        try:
            await database.execute(
                """INSERT INTO x402_payment_requests (
                       id, user_id, amount, amount_usd, asset, chain, recipient, nonce,
                       deadline, status, metadata,
                       asset_id, asset_address, asset_decimals, amount_raw,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             datetime('now'), datetime('now'))""",
                # ⚠️ The LEGACY `asset` column keeps its lowercase symbol. Every
                # existing reader (api/x402_endpoints.py, tools/x402/service.py)
                # displays it, and a USDC invoice must keep reading "usdc".
                (request_id, column_user, str(final_amount), final_amount,
                 (asset.symbol or "usdc").lower(), chain,
                 normalize_recipient(recipient, chain), nonce, deadline,
                 "pending", metadata,
                 asset.asset_id, asset.address, asset.decimals, str(final_raw)),
            )
        except sqlite3.IntegrityError as e:
            # Task 14 review Finding 2 (duplicate-renewal TOCTOU): with
            # `subscription_id` set, the ONLY unique constraint this INSERT can
            # plausibly hit (besides an astronomically-unlikely random-uuid
            # nonce collision) is
            # `idx_x402_requests_pending_subscription_unique` — the partial
            # index enforcing at most one PENDING renewal invoice per
            # subscription (self-healed by
            # `modules.database.x402_tables.dedupe_and_create_subscription_pending_unique_index`).
            # Two concurrent watcher instances (`UVICORN_WORKERS>1`) can both
            # pass `subscriptions._has_open_renewal_invoice`'s plain SELECT
            # before either has inserted; the index is the atomic backstop —
            # the loser must be treated as "a pending renewal already exists",
            # never crash/propagate a raw IntegrityError to the caller.
            if subscription_id and "idx_x402_requests_pending_subscription_unique" in str(e):
                logger.info(
                    "x402 renewal-invoice create for subscription %s hit the "
                    "pending-renewal unique index (a pending renewal invoice "
                    "already exists) — refusing to create a duplicate, "
                    "concurrent-create TOCTOU closed by the index", subscription_id)
                raise ValueError(
                    f"a pending renewal invoice already exists for subscription "
                    f"{subscription_id} — refusing to create a second"
                ) from e
            raise

    # Task 11: keep on-chain amount-matching unambiguous. Inert (amount_usd
    # untouched) unless on-chain detection is on (`_jitter_should_apply` —
    # the I2 fix forces jitter on whenever detection is on, regardless of the
    # jitter flag). I1 fix: the collision-check (SELECT) and the INSERT are
    # two separate awaited DB calls — without a lock, two concurrent creates
    # for the SAME treasury+amount can both observe "no collision" before
    # either has inserted, so BOTH keep the exact amount (defeating the
    # jitter). Hold the per-treasury lock across the dedupe SELECT + INSERT
    # so the second racer's SELECT always sees the first racer's row.
    if amount_raw is not None:
        # 046 phase 2: a PINNED raw amount cannot be separated by the USD jitter
        # below — `_insert` writes `final_raw = int(amount_raw)` when the caller
        # pins one, so the jitter moved only the displayed dollars while the
        # payable integer (the one the settlement match compares, exactly,
        # oldest-first) stayed identical. Two same-price room offers therefore
        # shared an amount and one payment applied the OTHER payer's action
        # against a different person. The RAW is nudged instead, always — a room
        # action is identified BY its amount, so this may not depend on the
        # detection flag. The loop, the lock, the index and the retry live in
        # `invoice_jitter` (this file is under a size ratchet for good reason).
        from modules.x402.invoice_jitter import insert_with_unique_raw

        async def _insert_at(_raw: int) -> None:
            nonlocal amount_raw
            amount_raw = _raw       # read back by `_insert`'s closure
            await _insert(amount_usd)

        amount_raw = await insert_with_unique_raw(
            _insert_at, int(raw), normalize_recipient(recipient, chain),
            database, asset_address=asset.address, decimals=asset.decimals)
    elif _jitter_should_apply(chain):
        # M5: the partial UNIQUE index is the CROSS-process backstop for the
        # in-process dedupe below. Created only here (jitter-active path).
        await _ensure_pending_amount_unique_index(database)
        async with _treasury_lock(normalize_recipient(recipient, chain)):
            candidate = await _dedupe_amount_for_treasury(
                amount_usd, normalize_recipient(recipient, chain), cap, database,
                asset_address=asset.address, decimals=asset.decimals, kind=kind)
            # The dedupe SELECT closes the SAME-process TOCTOU; the index closes
            # the CROSS-process one (workers>1). If a concurrent worker inserted
            # this exact (recipient, amount) between our SELECT and INSERT, the
            # INSERT raises IntegrityError on the pending-amount index — bump to
            # the next jitter candidate and retry, exactly as the in-process
            # dedupe would have (M5 IntegrityError-retry).
            attempts = 0
            while True:
                try:
                    await _insert(candidate)
                    break
                except sqlite3.IntegrityError as e:
                    if not _is_pending_amount_conflict(e):
                        raise
                    attempts += 1
                    if attempts > 100:
                        raise ValueError(
                            "could not find a unique pending-invoice amount for "
                            "this treasury after 100 jitter steps (too many "
                            "same-amount pending invoices)")
                    candidate = round(candidate + 0.0001, 6)
                    if candidate > cap:
                        raise ValueError(
                            f"amount ${candidate:.4f} exceeds the invoice ceiling "
                            f"${cap:.2f} while jittering past a same-amount "
                            "collision (X402_INVOICE_MAX_USD)")
            amount_usd = candidate
    else:
        # The M5 partial-unique index (`_PENDING_AMOUNT_INDEX`) is created
        # ONLY on the jitter-active path above — but once created it is never
        # dropped when jitter/detection is later disabled (self-healing
        # `CREATE ... IF NOT EXISTS`, no matching DROP). So a deployment that
        # enabled detection, created the index, then disabled it again can
        # still hit that RESIDUAL index here: a same-(recipient, amount_usd)
        # pending invoice raises a raw sqlite3.IntegrityError out of `_insert`
        # (its own internal catch only recognizes the subscription-renewal
        # index, so this one falls through to `raise`). Fail closed with the
        # same clean, agent-readable refusal the jitter-cap path uses instead
        # of leaking a raw traceback.
        try:
            await _insert(amount_usd)
        except sqlite3.IntegrityError as e:
            if _is_pending_amount_conflict(e):
                raise ValueError(
                    f"a pending agent invoice for ${amount_usd:.2f} to this "
                    "treasury already exists — refusing to create a "
                    "duplicate (same-amount pending-invoice guard); use a "
                    "different amount or wait for the existing invoice to "
                    "resolve"
                ) from e
            raise

    _emit("payment_requested", user_id=user_id, session_id=session_id, attrs={
        "request_id": request_id, "amount_usd": amount_usd, "chain": chain,
        "purpose": purpose.strip()[:200], "deadline": deadline,
    })
    final_raw = (int(amount_raw) if amount_raw is not None
                 else _raw_for(amount_usd))
    return {
        "request_id": request_id,
        "amount_usd": amount_usd,
        "asset": (asset.symbol or "usdc").lower(),
        "asset_id": asset.asset_id,
        "asset_symbol": asset.symbol,
        "asset_address": asset.address,
        "asset_decimals": asset.decimals,
        "amount_raw": final_raw,
        "chain": chain,
        "recipient": recipient,
        "purpose": purpose.strip(),
        "expires_at_epoch": deadline,
        "status": "pending",
        "payer_contact": contact,
    }


async def list_payment_requests(
    *, user_id: str, status: Optional[str] = None, limit: int = 20, db=None,
) -> List[Dict[str, Any]]:
    """Tenant-scoped invoice listing (newest first). Matches the tenant either by
    the user_id column or metadata.tenant_id (the FK-fallback path). An empty
    user_id returns nothing (the anonymous bucket is refused at creation too)."""
    if not user_id:
        return []
    database = await _resolve_db(db)
    if database is None:
        return []
    # The status filter must be applied in SQL BEFORE the LIMIT — filtering in
    # Python after a `ORDER BY created_at DESC LIMIT n` would grab only the newest
    # n rows of ANY status and could return [] while older matching (e.g. still
    # unpaid pending) invoices exist beyond the window.
    where = ["(user_id = ? OR json_extract(metadata, '$.tenant_id') = ?)",
             "json_extract(metadata, '$.kind') = ?"]
    params: List[Any] = [user_id, user_id, INVOICE_KIND]
    if status:
        where.append("status = ?")
        params.append(status)
    params.append(max(1, int(limit)))
    rows = await database.fetch_all(
        f"""SELECT * FROM x402_payment_requests
           WHERE {' AND '.join(where)}
           ORDER BY created_at DESC LIMIT ?""",
        tuple(params),
    )
    out = []
    for row in rows or []:
        meta = _row_metadata(row)
        out.append({
            "request_id": row.get("id"),
            "amount_usd": row.get("amount_usd"),
            "status": row.get("status"),
            "purpose": meta.get("purpose"),
            "payer_contact": meta.get("payer_contact") or meta.get("payer_hint"),
            "session_id": meta.get("session_id"),
            "chain": row.get("chain"),
            "created_at": row.get("created_at"),
            "completed_at": row.get("completed_at"),
            "deadline": row.get("deadline"),
        })
    return out


async def get_payment_request(request_id: str, *, db=None) -> Optional[Dict[str, Any]]:
    """Read one invoice row by id — the payable endpoint needs the amount, chain,
    recipient and status to build a per-invoice PaymentRequirements challenge.

    Public (no tenant scoping): a third-party payer legitimately does not know the
    invoice's owning tenant. Returns None for a missing row or a non-invoice row."""
    if not request_id:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE id = ?", (request_id,))
    if not row or not is_invoice_row(row):
        return None
    meta = _row_metadata(row)
    return {
        "request_id": row.get("id"),
        "amount_usd": row.get("amount_usd"),
        "asset": row.get("asset"),
        # 046: WHICH token, at what precision, in what raw amount. Without
        # these the public challenge reads every invoice as usdc-base and
        # serves a facilitator shape for an asset no facilitator can settle.
        "asset_id": row.get("asset_id"),
        "asset_address": row.get("asset_address"),
        "asset_decimals": row.get("asset_decimals"),
        "amount_raw": row.get("amount_raw"),
        "chain": row.get("chain"),
        "recipient": row.get("recipient"),
        "nonce": row.get("nonce"),
        "deadline": row.get("deadline"),
        "status": row.get("status"),
        "purpose": meta.get("purpose") or "",
        "payer_contact": meta.get("payer_contact") or meta.get("payer_hint"),
        "kind": meta.get("kind") or INVOICE_KIND,
        "room_action": meta.get("room_action"),
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
    }


async def get_payment_request_by_tx_hash(transaction_hash: str, *, db=None) -> Optional[Dict[str, Any]]:
    """Read one invoice row by its settlement transaction hash — the
    counterpart to :func:`get_payment_request` for callers that only carry a
    ``transaction_hash`` reference, not the ``request_id`` (Task 15, Phase 4:
    ERC-8004 ``ProofOfPayment`` only carries a ``txHash``, never the invoice's
    own id — see ``modules/eip8004/reputation.py::_verify_payment_proof``).

    ``transaction_hash`` carries a partial UNIQUE index (see
    :func:`settle_payment_request`), so at most one row can ever match — this
    is a safe, unambiguous lookup. Returns ``None`` for a missing/empty hash
    or a non-invoice row (mirrors :func:`get_payment_request`'s
    ``is_invoice_row`` filter); the caller decides what "not found" means
    (typically: refuse to treat the referencing proof as verified).

    M1: normalized (:func:`_norm_tx`) before both the emptiness guard and the
    query parameter — a caller passing a mixed-case/checksummed hash must
    still find a row stored (post-backfill) in lowercase."""
    transaction_hash = _norm_tx(transaction_hash)
    if not transaction_hash:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE transaction_hash = ?",
        (transaction_hash,))
    if not row or not is_invoice_row(row):
        return None
    meta = _row_metadata(row)
    return {
        "request_id": row.get("id"),
        "amount_usd": row.get("amount_usd"),
        "asset": row.get("asset"),
        "chain": row.get("chain"),
        "recipient": row.get("recipient"),
        "nonce": row.get("nonce"),
        "deadline": row.get("deadline"),
        "status": row.get("status"),
        "transaction_hash": row.get("transaction_hash"),
        "purpose": meta.get("purpose") or "",
        "payer_contact": meta.get("payer_contact") or meta.get("payer_hint"),
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
    }


async def get_invoice_tenant(request_id: str, *, db=None) -> Optional[str]:
    """The tenant (user_id) that actually owns this invoice — read directly
    from ITS OWN row (``metadata.tenant_id``, falling back to the ``user_id``
    column for the FK-fallback storage case), independent of anything a
    caller might separately claim.

    Task 14 review Finding 3 (cheap defense-in-depth):
    ``modules.x402.subscriptions.apply_settlement`` uses this to confirm a
    settled invoice's tenant actually matches the subscription it is about to
    extend, before applying the extension — unexploitable today (only the
    settlement watcher ever writes ``metadata.subscription_id``, always
    matching the invoice's own tenant), but cheap insurance against a
    tenant-A invoice silently extending a tenant-B subscription. Returns
    ``None`` for a missing row (permissive — the caller treats "can't
    resolve" as "nothing to compare against", not as a mismatch)."""
    if not request_id:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE id = ?", (request_id,))
    if not row:
        return None
    meta = _row_metadata(row)
    return meta.get("tenant_id") or row.get("user_id") or None


async def claim_for_settlement(request_id: str, *, db=None) -> bool:
    """Atomic pending→settling CAS — the exclusive right to settle ONE invoice.

    The payable endpoint claims BEFORE calling the facilitator so two concurrent
    distinct payers can never both settle the same invoice on-chain: the loser's
    claim fails (rowcount 0) and it never touches the facilitator. On a facilitator
    failure the winner reverts via :func:`revert_settlement_claim`."""
    database = await _resolve_db(db)
    if database is None:
        return False
    cur = await database.execute(
        "UPDATE x402_payment_requests SET status = 'settling', updated_at = datetime('now') "
        "WHERE id = ? AND status = 'pending'",
        (request_id,),
    )
    return bool(getattr(cur, "rowcount", 0))


async def revert_settlement_claim(request_id: str, *, db=None) -> None:
    """Revert settling→pending after a facilitator/verify failure (payable again).

    Only a row THIS caller left in 'settling' is reverted (guarded on status), so a
    concurrently-completed row is never resurrected."""
    database = await _resolve_db(db)
    if database is None:
        return
    await database.execute(
        "UPDATE x402_payment_requests SET status = 'pending', updated_at = datetime('now') "
        "WHERE id = ? AND status = 'settling'",
        (request_id,),
    )


async def revert_stale_settling(*, max_age_seconds: int = 600, db=None,
                                ) -> List[Dict[str, Any]]:
    """H7 stale-``settling`` reaper: heal invoices STRANDED in ``settling``.

    A ``claim_for_settlement`` flips ``pending -> settling`` BEFORE the
    facilitator round-trip; ``settle_payment_request`` then flips
    ``settling -> completed``. If the settling task is cancelled/crashes in
    between (client disconnect cancels the request task; a process crash), the
    row is stranded in ``settling`` forever — ``expire_stale_requests`` only
    ever touches ``pending`` rows, and nothing else re-checks ``settling``.

    A genuine settle completes well within the 300s facilitator timeout, so a
    row still ``settling`` past ``max_age_seconds`` (default 600s = 10min) is
    stranded. Revert it to ``pending`` (payable + expirable again) UNLESS it
    already carries a ``transaction_hash`` that ALREADY settled some invoice
    (defensive — a ``settling`` row should never carry one, but never resurrect
    a genuine settle). Returns the reverted invoice dicts so the caller can emit
    an owner notice. The tx-hash uniqueness guard in ``settle_payment_request``
    means a reverted-then-re-paid invoice can never double-settle on-chain."""
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        """SELECT * FROM x402_payment_requests
           WHERE status = 'settling'
             AND updated_at < datetime('now', ?)
             AND json_extract(metadata, '$.kind') = ?""",
        (f'-{int(max(1, max_age_seconds))} seconds', INVOICE_KIND),
    )
    reverted = []
    for row in rows or []:
        tx = _norm_tx(row.get("transaction_hash"))
        if tx and await transaction_hash_already_settled(tx, db=database):
            # Genuinely settled elsewhere (should be 'completed', not 'settling')
            # — never resurrect it back to pending.
            continue
        cur = await database.execute(
            "UPDATE x402_payment_requests SET status='pending', updated_at=datetime('now') "
            "WHERE id = ? AND status = 'settling'",
            (row["id"],),
        )
        if not getattr(cur, "rowcount", 0):
            continue  # lost a race to a concurrent settle — leave it
        meta = _row_metadata(row)
        reverted.append({
            "request_id": row["id"],
            "amount_usd": row.get("amount_usd"),
            "session_id": meta.get("session_id") or "",
            "user_id": meta.get("tenant_id") or row.get("user_id") or "",
            "purpose": meta.get("purpose") or "",
        })
    if reverted:
        logger.warning(
            "x402 invoicing: reverted %d invoice(s) stranded in 'settling' past "
            "%ds back to 'pending' (H7 stale-settling reaper): %s",
            len(reverted), max_age_seconds, [r["request_id"] for r in reverted])
    return reverted


async def transaction_hash_already_settled(transaction_hash: str, *, db=None) -> bool:
    """True when `transaction_hash` has already settled SOME invoice (Task 11
    C2 fix). A given on-chain transfer must settle AT MOST ONE invoice EVER:
    without this guard, a settlement-watcher failure that leaves the scan
    checkpoint un-advanced (`SettlementWatcher._scan_onchain` only advances it
    AFTER the full block range is processed) could cause a LATER tick to
    re-process the SAME already-consumed transfer against a DIFFERENT
    (now-unrelated, same-amount) pending invoice — silently redirecting a real
    payer's funds to settle someone else's bill. `transaction_hash` is only
    ever stamped by a successful `settle_payment_request` call, and a
    completed row can never leave that terminal status, so a bare existence
    check is sufficient (no status filter needed).

    M1: normalized (:func:`_norm_tx`) before the query parameter — a
    lowercase on-chain hash and a mixed-case facilitator hash for the SAME
    transfer must compare equal."""
    transaction_hash = _norm_tx(transaction_hash)
    if not transaction_hash:
        return False
    database = await _resolve_db(db)
    if database is None:
        return False
    row = await database.fetch_one(
        "SELECT 1 AS ok FROM x402_payment_requests WHERE transaction_hash = ? LIMIT 1",
        (transaction_hash,),
    )
    return bool(row)


async def settle_payment_request(
    request_id: str, *, transaction_hash: Optional[str] = None, db=None,
) -> bool:
    """Attested transition to completed (owner CLI / API). Idempotent: only a
    'pending' (owner-attested direct settle) or 'settling' (endpoint post-claim) row
    transitions; anything else returns False. The settlement WAKE + payment_settled
    event are the watcher's job (it runs in the agent process; this may not).

    Task 11 C2 fix: when `transaction_hash` is given, it must never settle a
    SECOND invoice — checked via `transaction_hash_already_settled` BEFORE the
    UPDATE (the primary, always-active guard). The partial UNIQUE index on
    `x402_payment_requests.transaction_hash` (self-healed in
    `X402Tables.create_tables`; also tracked by migration v1.6.0) is
    defense-in-depth against a genuine concurrent-write race this pre-check
    alone can't close — a `sqlite3.IntegrityError` from that race is treated
    the same as "refused", never raised past this function.

    M1: `transaction_hash` is normalized (:func:`_norm_tx`) ONCE here, up
    front, so the SAME lowercase form is used for the guard call AND stamped
    by the UPDATE below — a mixed-case facilitator hash is stored (and
    matched) canonically."""
    transaction_hash = _norm_tx(transaction_hash)
    database = await _resolve_db(db)
    if database is None:
        return False
    if transaction_hash and await transaction_hash_already_settled(
            transaction_hash, db=database):
        logger.warning(
            "x402 settle refused: tx %s already settled a DIFFERENT invoice "
            "(request %s NOT settled — replay guard)", transaction_hash, request_id)
        return False
    try:
        cur = await database.execute(
            """UPDATE x402_payment_requests
               SET status = 'completed', transaction_hash = ?,
                   completed_at = datetime('now'), updated_at = datetime('now')
               WHERE id = ? AND status IN ('pending', 'settling')""",
            (transaction_hash, request_id),
        )
    except sqlite3.IntegrityError:
        # Lost a genuine concurrent race to the UNIQUE index — two settle
        # attempts for the same tx_hash landed together. Never resurrect or
        # half-apply; the row is left untouched.
        logger.warning(
            "x402 settle refused: tx %s hit the transaction_hash uniqueness "
            "guard concurrently (request %s NOT settled)", transaction_hash, request_id)
        return False
    settled = bool(getattr(cur, "rowcount", 0))
    if settled:
        logger.info("x402 invoice %s settled (tx=%s)", request_id, transaction_hash)
    return settled


async def expire_stale_requests(*, db=None, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """pending + past deadline → expired. Returns the expired invoice dicts and
    emits one payment_expired event each."""
    database = await _resolve_db(db)
    if database is None:
        return []
    cutoff = int(now if now is not None else time.time())
    rows = await database.fetch_all(
        """SELECT * FROM x402_payment_requests
           WHERE status = 'pending' AND deadline < ?
             AND json_extract(metadata, '$.kind') = ?""",
        (cutoff, INVOICE_KIND),
    )
    expired = []
    for row in rows or []:
        cur = await database.execute(
            "UPDATE x402_payment_requests SET status='expired', updated_at=datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        if not getattr(cur, "rowcount", 0):
            # lost the race to a concurrent settle — not actually expired; never
            # emit a false payment_expired for a payment that was in fact paid
            continue
        meta = _row_metadata(row)
        tenant = meta.get("tenant_id") or row.get("user_id") or ""
        _emit("payment_expired", user_id=tenant,
              session_id=meta.get("session_id") or "", attrs={
                  "request_id": row["id"], "amount_usd": row.get("amount_usd")})
        expired.append({"request_id": row["id"], "amount_usd": row.get("amount_usd"),
                        "session_id": meta.get("session_id"), "user_id": tenant,
                        "purpose": meta.get("purpose")})
    return expired


async def settled_unnotified_invoices(*, db=None) -> List[Dict[str, Any]]:
    """Settled agent invoices whose originating session has not been woken yet.

    M3 (audit 2026-08-22): this used to match the spaced literal
    ``'%"wake_delivered": false%'`` via ``LIKE``. Normal rows are written
    spaced by ``json.dumps``, but ANY later ``json_set`` on the metadata blob
    (e.g. the boot-time subscription dedup in
    ``modules.database.x402_tables.dedupe_and_create_subscription_pending_unique_index``)
    re-serializes the WHOLE blob compactly (``"wake_delivered":false``), and
    the spaced LIKE silently stopped matching — dropping the wake forever.
    ``json_extract`` reads the value regardless of the blob's spacing.
    ``json_extract`` on a JSON boolean returns SQLite integer ``0``/``1``, so
    compare against ``0`` (not the string ``'false'``). A row where the key
    is ABSENT returns SQL ``NULL``, which ``= 0`` does not match — this is
    deliberately preserved (absent ⇒ not eligible), matching the old LIKE's
    behaviour on such rows."""
    database = await _resolve_db(db)
    if database is None:
        return []
    # 046: every PAYABLE kind, not just `agent_invoice`. A producer missing
    # from this filter mints rows that take real money and are then never
    # notified, never actuated, and invisible to the owner.
    placeholders = ",".join("?" for _ in PAYABLE_KINDS)
    rows = await database.fetch_all(
        f"""SELECT * FROM x402_payment_requests
            WHERE status IN ('completed', 'settled_no_tx')
              AND json_extract(metadata, '$.kind') IN ({placeholders})
              AND json_extract(metadata, '$.wake_delivered') = 0""",
        tuple(PAYABLE_KINDS),
    )
    out = []
    for row in rows or []:
        meta = _row_metadata(row)
        out.append({
            "request_id": row["id"],
            "amount_usd": row.get("amount_usd"),
            "transaction_hash": row.get("transaction_hash"),
            "session_id": meta.get("session_id") or "",
            "user_id": meta.get("tenant_id") or row.get("user_id") or "",
            "purpose": meta.get("purpose") or "",
            "correspondent_ref": meta.get("correspondent_ref") or None,
            "subscription_id": meta.get("subscription_id") or None,
            # 046: what this payment BOUGHT, when it bought something.
            "kind": meta.get("kind") or INVOICE_KIND,
            "room_action": meta.get("room_action") or None,
        })
    return out


async def expired_unnotified_invoices(*, db=None) -> List[Dict[str, Any]]:
    """Expired agent invoices whose originating session/owner has not been
    notified yet (G-22 — an unpaid invoice must not silently vanish).

    Mirrors :func:`settled_unnotified_invoices` but reads ``status='expired'``
    rows instead. Reuses the SAME ``wake_delivered`` metadata flag rather than
    a second ``expiry_wake_delivered`` marker: a row is either settled
    (``completed``/``settled_no_tx``) or expired — two MUTUALLY EXCLUSIVE
    terminal states (``expire_stale_requests`` only ever touches
    ``status='pending'`` rows, and a row that has reached one terminal status
    can never transition to the other — see :func:`settle_payment_request` /
    :func:`expire_stale_requests`'s status guards). Since this query and
    :func:`settled_unnotified_invoices` are already partitioned by ``status``,
    one shared flag is sufficient and simpler than two: a settled row can
    never appear here, an expired row can never appear there, so neither
    terminal wake can suppress the other."""
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        """SELECT * FROM x402_payment_requests
           WHERE status = 'expired'
             AND json_extract(metadata, '$.kind') = ?
             AND json_extract(metadata, '$.wake_delivered') = 0""",
        (INVOICE_KIND,),
    )
    out = []
    for row in rows or []:
        meta = _row_metadata(row)
        out.append({
            "request_id": row["id"],
            "amount_usd": row.get("amount_usd"),
            "session_id": meta.get("session_id") or "",
            "user_id": meta.get("tenant_id") or row.get("user_id") or "",
            "purpose": meta.get("purpose") or "",
            "correspondent_ref": meta.get("correspondent_ref") or None,
        })
    return out


async def claim_wake(request_id: str, *, db=None) -> bool:
    """Atomically claim the settlement notification for one invoice.

    M3 (audit 2026-08-22): this used to do a string ``REPLACE`` on the
    machine-written ``"wake_delivered": false`` token, guarded by a LIKE on
    the same spaced token. A ``json_set`` elsewhere (e.g. the boot-time
    subscription dedup) re-serializes the WHOLE metadata blob compactly,
    which the spaced LIKE then never matched again — the claim silently
    always failed for such a row. Now uses ``json_set``/``json_extract``,
    which are agnostic to the blob's whitespace. ``json_set(..., json('true'))``
    (NOT the bare number ``1``) keeps the stored shape a genuine JSON boolean
    so ``json_extract(...) = 0`` keeps working for the still-false case.

    Still a single atomic ``UPDATE ... WHERE id = ? AND
    json_extract(metadata, '$.wake_delivered') = 0``, checked via rowcount —
    so when two watcher processes race, exactly ONE wins the claim and
    delivers the wake/event (claim-then-notify, never notify-then-mark)."""
    database = await _resolve_db(db)
    if database is None:
        return False
    cur = await database.execute(
        """UPDATE x402_payment_requests
           SET metadata = json_set(metadata, '$.wake_delivered', json('true')),
               updated_at = datetime('now')
           WHERE id = ? AND json_extract(metadata, '$.wake_delivered') = 0""",
        (request_id,),
    )
    return bool(getattr(cur, "rowcount", 0))


async def mark_wake_delivered(request_id: str, *, db=None) -> None:
    """Back-compat wrapper over :func:`claim_wake` (ignores the claim result)."""
    await claim_wake(request_id, db=db)


async def claim_expiry_wake(request_id: str, *, db=None) -> bool:
    """Atomically claim the EXPIRY notification for one invoice (G-22).

    Deliberately delegates to :func:`claim_wake` — same atomic CAS over the
    SAME ``wake_delivered`` metadata flag. See :func:`expired_unnotified_invoices`
    for why sharing the flag with settlement is safe (mutually exclusive
    terminal ``status`` values, both watcher queries already status-partitioned).
    Kept as a distinctly-named function — rather than callers reusing
    ``claim_wake`` directly — for call-site clarity/symmetry with
    :func:`claim_wake`, and so the two notification paths can diverge later
    without a shared-name landmine."""
    return await claim_wake(request_id, db=db)


# --- Task 11 (Phase 2): on-chain settlement detection --------------------
# A per-treasury scan checkpoint + an amount-based pending-invoice matcher,
# consumed by `modules.x402.settlement_watcher.SettlementWatcher._scan_onchain`.
# The `settlement_scan` table is created by `modules.database.x402_tables
# .X402Tables.create_tables` (same runtime CREATE-IF-NOT-EXISTS pattern the
# other x402 tables use).

async def get_scan_checkpoint(treasury: str, *, db=None) -> Optional[int]:
    """Last fully-scanned block for this treasury, or None if never scanned."""
    if not treasury:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        "SELECT last_block FROM settlement_scan WHERE treasury = ?", (treasury,))
    if not row or row.get("last_block") is None:
        return None
    return int(row["last_block"])


async def advance_scan_checkpoint(treasury: str, last_block: int, *, db=None) -> None:
    """Persist the new last-scanned block for this treasury. Never regresses
    the stored checkpoint (guards a stray out-of-order/concurrent call)."""
    if not treasury:
        return
    database = await _resolve_db(db)
    if database is None:
        return
    await database.execute(
        """INSERT INTO settlement_scan (treasury, last_block, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(treasury) DO UPDATE SET
               last_block = excluded.last_block, updated_at = excluded.updated_at
           WHERE excluded.last_block > settlement_scan.last_block""",
        (treasury, int(last_block)),
    )


# 046: asset resolution and asset-keyed settlement matching live in their own
# module (the decomposition rule + this file's size ratchet). Re-exported here
# so every pre-existing caller keeps working unchanged.
from modules.x402.invoice_assets import (  # noqa: E402,F401
    atomic_amount, match_pending_invoice, match_pending_invoice_by_amount,
    resolve_invoice_asset,
)


# 046: the amount-jitter concern lives in its own module (the decomposition rule
# + this file's size ratchet). Re-exported so every pre-existing caller and test
# keeps working unchanged.
from modules.x402.invoice_jitter import (  # noqa: E402,F401
    _PENDING_AMOUNT_INDEX, _dedupe_amount_for_treasury,
    _ensure_pending_amount_unique_index, _is_pending_amount_conflict,
    _treasury_lock,
)
