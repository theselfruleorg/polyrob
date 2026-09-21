"""
x402 payment integration using official fastapi-x402 library.

This module provides the integration layer between the official x402 protocol
(via fastapi-x402) and POLYROB's user/payment system.

The fastapi-x402 library handles:
- Payment verification via Coinbase facilitator (https://api.cdp.coinbase.com)
- On-chain settlement (actual USDC transfer)
- Payment payload encoding/decoding

POLYROB's integration layer handles:
- User profile creation for x402 payers
- Mapping wallet addresses to user IDs
- Recording payments in our database
"""

import os
import time
import json
import logging
import hashlib
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


# Relocated to core.identity (polyrob-core). Re-exported here for back-compat so the
# platform x402 receive layer and existing callers keep importing from this module.
from core.identity import generate_user_id_from_wallet  # noqa: F401


async def ensure_user_profile_for_payer(wallet_address: str, user_id: str) -> bool:
    """Ensure a user_profiles record exists for x402 payer.

    Creates a new user profile if one doesn't exist for this wallet.

    Args:
        wallet_address: Payer's wallet address
        user_id: Generated user ID

    Returns:
        True if profile exists or was created, False on error
    """
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()
        db = container.get_service('database_manager')

        if not db:
            logger.warning("Database not available for user profile creation")
            return False

        # Check if user already exists
        existing = await db.fetch_one(
            "SELECT user_id FROM user_profiles WHERE wallet_address = ?",
            (wallet_address.lower(),)
        )

        if existing:
            return True

        # Create new user profile for x402 payer
        await db.execute("""
            INSERT INTO user_profiles (
                user_id, wallet_address, role, tier,
                created_at, updated_at
            ) VALUES (?, ?, 'user', 'x402', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (user_id, wallet_address.lower()))

        # Also create initial credit balance (0 credits for x402 users)
        await db.execute("""
            INSERT OR IGNORE INTO user_credits (user_id, balance, lifetime_earned, lifetime_spent)
            VALUES (?, 0, 0, 0)
        """, (user_id,))

        logger.info(f"Created user profile for x402 payer: {user_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to create user profile for x402 payer: {e}")
        return False


def settlement_payment_id(
    transaction_hash: Optional[str],
    payer_address: str,
    resource_path: str,
    minute_bucket: int,
) -> str:
    """Build a deterministic payment id for a settled x402 request.

    Uses the on-chain tx hash when present (1 tx == 1 record). When the
    facilitator reports success WITHOUT a tx hash, derive a stable surrogate from
    (payer, resource, minute) so revenue is still recorded and a same-window
    retry of the identical settlement dedups instead of double-recording.
    """
    if transaction_hash:
        return f"x402_{transaction_hash[:16]}"
    digest = hashlib.sha256(
        f"{payer_address.lower()}:{resource_path}:{minute_bucket}".encode()
    ).hexdigest()[:16]
    return f"x402_notx_{digest}"


async def record_x402_payment(
    payment_id: str,
    wallet_address: str,
    user_id: str,
    amount_usd: float,
    network: str,
    recipient: str,
    transaction_hash: Optional[str] = None,
    nonce: Optional[str] = None,
    amount_atomic: Optional[str] = None,
    deadline: Optional[int] = None,
    asset: str = "usdc",
    tenant_id: Optional[str] = None,
    db=None,
) -> bool:
    """Record a settled x402 payment in our database.

    N1 fix: the previous INSERT omitted four NOT NULL columns
    (``amount``, ``recipient``, ``nonce``, ``deadline``), so every insert raised
    a constraint violation that was swallowed -> the agent settled USDC on-chain
    and persisted nothing. This now supplies every NOT NULL column and is
    idempotent on the unique ``nonce`` (a replayed on-chain tx is a no-op).

    043 A20 fix: a machine payer settles under its own derived ``usr_<hex>``
    ``user_id``, which never matches the unified ledger's tenant predicate
    (``user_id = ? OR json_extract(metadata, '$.tenant_id') = ?``) for the
    OWNER tenant — so machine income (A2A / /v1 billed routes) rendered as
    $0.00 on ``/finance``/``/status`` even though it settled. ``tenant_id``
    (the bound owner principal) is now stamped into ``metadata`` alongside the
    payer's own id, the same way agent invoices already carry
    ``metadata.tenant_id`` — this makes the row match the SAME predicate.

    Args:
        payment_id: Unique payment identifier (also the row primary key).
        wallet_address: Payer's wallet address.
        user_id: User ID associated with the payment.
        amount_usd: Payment amount in USD.
        network: Blockchain network (e.g. 'base').
        recipient: Treasury / pay-to address that received funds.
        transaction_hash: On-chain settlement tx hash (None if the facilitator
            reported success without a tx).
        nonce: Dedup key. Defaults to ``transaction_hash`` then ``payment_id``.
        amount_atomic: Atomic (base-unit) amount string. Defaults to ``amount_usd``.
        deadline: Settlement deadline epoch seconds. Defaults to now.
        asset: Settled asset symbol.
        tenant_id: Owner tenant this income should be attributed to (the
            payer's own derived id is NOT a tenant an owner ledger reads).
            None/"" is stored honestly as "" — the row then matches no tenant's
            ledger rather than being silently mis-attributed.
        db: Optional database handle (mirrors ``invoicing.create_payment_request``'s
            ``db=`` pattern); resolves the container's ``database_manager`` when
            omitted, so every existing caller is unaffected.

    Returns:
        True if recorded (or already recorded), False on error.
    """
    try:
        from modules.x402._db import resolve_db
        db = await resolve_db(db)

        if not db:
            logger.warning("Database not available for payment recording")
            return False

        # Never drop revenue: a tx-less settlement still gets a row, flagged for
        # the reconciliation job, with a deterministic surrogate dedup key.
        if transaction_hash:
            status = "completed"
            resolved_nonce = nonce or transaction_hash
        else:
            status = "settled_no_tx"
            resolved_nonce = nonce or payment_id

        resolved_amount = amount_atomic if amount_atomic is not None else str(amount_usd)
        resolved_deadline = deadline if deadline is not None else int(time.time())

        # Same shape as invoicing.create_payment_request's metadata.tenant_id
        # (kind="agent_invoice" there, "machine_payment" here) -> ONE predicate
        # (modules/credits/unified_ledger.py::_inbound_leg) reads both kinds of
        # inbound row the same way.
        metadata = json.dumps({
            "kind": "machine_payment",
            "tenant_id": tenant_id or "",
            "payer_user_id": user_id,
        })

        # Address normalization goes through THE one chain-aware function —
        # this row shares x402_payment_requests.recipient with invoicing, and a
        # bare .lower() would destroy a base58 (SVM) address the moment this
        # path gains a non-EVM network (the 1dadf3ad landmine, second writer).
        from modules.x402.invoicing import normalize_recipient

        # Bare ON CONFLICT DO NOTHING makes a replayed PK/nonce a no-op while a
        # NOT NULL violation still RAISES (so a future missing-column bug is loud,
        # not silently swallowed like N1).
        await db.execute("""
            INSERT INTO x402_payment_requests (
                id, user_id, payer_address, amount, amount_usd, asset, chain,
                recipient, nonce, deadline, status, transaction_hash, payment_id,
                metadata, created_at, completed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      datetime('now'), datetime('now'), datetime('now'))
            ON CONFLICT DO NOTHING
        """, (
            payment_id,
            user_id,
            normalize_recipient(wallet_address, network),
            resolved_amount,
            amount_usd,
            asset,
            network,
            normalize_recipient(recipient, network),
            resolved_nonce,
            resolved_deadline,
            status,
            transaction_hash,
            payment_id,
            metadata,
        ))

        logger.info(f"Recorded x402 payment: {payment_id} (${amount_usd}, {status})")
        return True

    except Exception as e:
        # Money already moved; do not crash the request, but make the loss LOUD
        # and reconcilable rather than silently returning False (N1 class).
        logger.error(
            f"x402.record_failed payment_id={payment_id} amount_usd={amount_usd} "
            f"tx={transaction_hash}: {e}"
        )
        return False


def get_x402_max_tokens_per_request() -> int:
    """Token budget one x402 request prepays for (default 200k).

    SSOT for BOTH the price (get_x402_price_usd) AND the runtime cap enforced in
    LLMUsageTracker — they MUST read the same value so the caller can never consume
    more (or less) tokens than the price covers. Config via X402_MAX_TOKENS_PER_REQUEST.
    """
    try:
        v = int(os.getenv("X402_MAX_TOKENS_PER_REQUEST", "200000"))
        return v if v > 0 else 200000
    except (TypeError, ValueError):
        return 200000


def _max_output_price_per_token() -> float:
    """Highest per-token OUTPUT price across the model registry (USD/token).

    Output is the priciest token class; billing the whole budget at this rate is the
    conservative worst case (before markup).
    """
    try:
        from modules.llm.model_registry import get_all_models
        prices = [
            m.pricing.output_price for m in get_all_models()
            if getattr(m, "pricing", None) and m.pricing and m.pricing.output_price
        ]
        return (max(prices) / 1_000_000.0) if prices else 0.0
    except Exception as e:
        logger.warning(f"could not read model pricing for x402 price derivation: {e}")
        return 0.0


def get_x402_price_usd() -> float:
    """Single source of truth for the x402 per-request price (USD).

    The live middleware charge, the /pricing endpoint and the Agent Card all read
    this so they can never diverge (P1-1).

    An explicit ``X402_PRICE_USD`` always wins. Otherwise the price is DERIVED from
    economics so a single request can never cost the platform more than it collects:

        price = X402_MAX_TOKENS_PER_REQUEST × (max model output rate) × X402_PRICE_MARKUP

    i.e. the WORST-CASE cost — every budgeted token billed at the most-expensive
    model's output rate — times a safety markup (default 2×). Because x402 settles
    BEFORE the request runs (pay-before-serve), we bill this worst case upfront; the
    runtime token cap (same X402_MAX_TOKENS_PER_REQUEST) guarantees actual usage can
    never exceed the budget, so the markup is a real margin, not a hope.
    """
    raw = os.environ.get("X402_PRICE_USD")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(f"Invalid X402_PRICE_USD={raw!r}; deriving price from model economics")

    try:
        budget = get_x402_max_tokens_per_request()
        markup = float(os.getenv("X402_PRICE_MARKUP", "2.0"))
        max_rate = _max_output_price_per_token()
        derived = budget * max_rate * markup
        if derived > 0:
            return round(derived, 6)
        logger.warning("x402 price derivation yielded 0 (no model pricing); using $0.01 fallback")
    except Exception as e:
        logger.warning(f"x402 price derivation failed ({e}); using $0.01 fallback")
    return 0.01


#: Post-settlement statuses that mean the SERVER refused a payer who had
#: already paid. 401/403 after a settled x402 payment is never the payer's
#: fault: the payment WAS the authentication, and an auth gate that then
#: refuses them took money for nothing (B1, 2026-09-21).
REFUNDABLE_AUTH_STATUSES = frozenset({401, 403})


def should_refund_on_status(status_code: int) -> bool:
    """Whether a downstream response status means the paid request failed.

    x402 settles BEFORE the downstream handler runs, so:

    - a server error (5xx) means the customer paid and got nothing;
    - a 401/403 means an auth gate downstream refused a caller whose payment
      had already settled — the x402 payment IS their credential, so this is
      the server's misconfiguration, not a client error.

    Every other 4xx (400/404/422/429) is the caller's own malformed or
    out-of-scope request and is not refundable here.
    """
    code = int(status_code)
    return code >= 500 or code in REFUNDABLE_AUTH_STATUSES


#: Telemetry kind emitted when a settled payment is flagged for refund.
#: The DB status alone is invisible: every invoice listing filters on
#: ``pending``/``completed``/``expired``, so a ``refund_due`` row disappears
#: from the four owner seats the moment it is written. A durable event is the
#: one place the fact survives a log rotation (2026-09-21 revalidation).
from core.event_kinds import PAYMENT_REFUND_DUE as PAYMENT_REFUND_DUE_EVENT  # the ONE literal


async def _tell_owner_refund_due(container: Any, payment_id: str) -> None:
    """Actively TELL the owner (2026-09-21 revalidation): the status figure and
    the critical lane were in place, but nothing sent the notice — the owner
    had to go and look at `/status`. Rides the ONE delivery rail on the
    critical lane (`payment_refund_due` is in `_CRITICAL_SOURCES`, so the
    daily cap never drops it). Never raises."""
    try:
        from core.instance import resolve_owner_user_id
        import core.surfaces.user_delivery as _ud
        owner = resolve_owner_user_id()
        if not owner:
            return
        text = (f"⚠ refund owed: a machine payment ({payment_id}) settled on-chain "
                f"and the request then failed, so I took money and delivered "
                f"nothing. It is listed under /invoices refund_due; the refund "
                f"is yours to make — nothing here sends money back on its own.")
        await _ud.deliver_user_message(container, owner, text,
                                       source=PAYMENT_REFUND_DUE_EVENT)
    except Exception:
        logger.warning("refund_due owner notice could not be delivered "
                       "(the status seat still shows it)", exc_info=True)


async def mark_payment_refund_due(payment_id: str) -> bool:
    """Flag an already-recorded x402 payment as refund_due (N4 reconciliation)."""
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()
        db = container.get_service('database_manager')
        if not db:
            logger.warning("Database not available to flag refund_due")
            return False
        await db.execute(
            "UPDATE x402_payment_requests SET status='refund_due', "
            "updated_at=datetime('now') WHERE id = ?",
            (payment_id,),
        )
        logger.warning(f"x402.refund_due payment_id={payment_id} (downstream failed after settlement)")
        _emit_refund_due(payment_id)
        await _tell_owner_refund_due(container, payment_id)
        return True
    except Exception as e:
        logger.error(f"Failed to flag x402 refund_due for {payment_id}: {e}")
        return False


def _emit_refund_due(payment_id: str) -> None:
    """Record the refund obligation as durable telemetry. Fail-open."""
    try:
        from core.instance import resolve_owner_user_id
        from modules.x402.invoicing import _emit

        try:
            owner = resolve_owner_user_id()
        except Exception:
            owner = ""
        _emit(PAYMENT_REFUND_DUE_EVENT, user_id=owner or "", session_id="",
              attrs={"payment_id": payment_id, "reason": "downstream_failed_after_settlement"})
    except Exception as e:  # telemetry never breaks the money path
        logger.debug("refund_due telemetry unavailable: %s", e)


# One-time WARN guard for a treasury/env vs wallet-address mismatch (W1.1) —
# module-level so every call site shares it; tests reset it directly.
_TREASURY_MISMATCH_WARNED = False


def resolve_treasury_address() -> str:
    """The ONE pay_to resolver (W1.1, 2026-08-21): explicit
    `X402_PAYMENT_RECIPIENT` always wins; when it is empty and
    `X402_TREASURY_FROM_WALLET` (default ON) and the agent wallet is enabled,
    the wallet's treasury-venue address fills in — read from memory at call
    time, NEVER written to any env file (the agent keeps zero write path to
    `/etc/polyrob/polyrob.env`). Fail-open to '' on any wallet fault, which
    preserves the legacy "no treasury configured" refusal downstream.

    When BOTH are set and differ, a one-time WARN fires: funds would land on
    the env address while wallet-based views watch the wallet address."""
    global _TREASURY_MISMATCH_WARNED
    explicit = os.environ.get("X402_PAYMENT_RECIPIENT", "").strip()

    wallet_addr = ""
    try:
        from core.env import bool_env
        if bool_env("X402_TREASURY_FROM_WALLET", True):
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
            if wallet is not None:
                # The TREASURY venue specifically — `wallet.address` is the
                # OPERATIONAL venue, which is a different key whenever
                # AGENT_WALLET_OPERATIONAL_VENUE=x402. `polyrob wallet init`
                # funds and pins the treasury address, so resolving anything
                # else would invoice an address the owner never funded.
                signer_for = getattr(wallet, "signer_for", None)
                if callable(signer_for):
                    wallet_addr = (signer_for("treasury").address or "").strip()
                else:
                    wallet_addr = (wallet.address or "").strip()
    except Exception:
        wallet_addr = ""

    if explicit:
        if (wallet_addr and wallet_addr.lower() != explicit.lower()
                and not _TREASURY_MISMATCH_WARNED):
            _TREASURY_MISMATCH_WARNED = True
            logger.warning(
                "X402_PAYMENT_RECIPIENT (%s) differs from the agent wallet's "
                "treasury address (%s) — invoices/challenges use the env value; "
                "wallet-based balance views watch the wallet address",
                explicit, wallet_addr)
        return explicit
    return wallet_addr


def receive_rail_summary() -> str:
    """One line describing THIS agent's receive rail, for agent-facing money
    views (§5.2, 2026-08-21).

    Why it exists: the historical "owner deploy package: mainnet-ready x402
    endpoint" goal was cancelled, but board dedup ignores cancelled rows, so
    nothing structurally stops the agent re-filing it. The durable fix is that
    the agent can SEE — where it already looks for money state — that it can
    already get paid with no endpoint at all, and that standing up an HTTP
    endpoint is owner-only work with a named runbook (never an agent goal).

    Pure read of config; returns '' on any fault (a status footer must never
    break the view it decorates)."""
    try:
        treasury = resolve_treasury_address()
        chain = os.environ.get("X402_DEFAULT_CHAIN", "base")
        parts = []
        if treasury:
            parts.append(f"treasury {treasury} on {chain}")
        else:
            parts.append(f"no treasury configured on {chain} — invoices refuse "
                         "until X402_PAYMENT_RECIPIENT or the agent wallet is set")

        from modules.x402.invoicing import x402_settle_onchain_detect_enabled
        parts.append("on-chain detect ON"
                     if x402_settle_onchain_detect_enabled() else "on-chain detect OFF")

        http_on = os.environ.get("X402_ENABLED", "false").lower() == "true"
        base_url = (os.environ.get("A2A_BASE_URL") or "").strip().rstrip("/")
        if http_on and base_url:
            parts.append(f"HTTP endpoint {base_url}")
        else:
            parts.append("no HTTP endpoint (not needed to get paid — invoice + "
                         "on-chain detect is the whole rail; standing one up is "
                         "owner-only work, run by the owner from a full repo "
                         "checkout: `scripts/setup_x402_endpoint.sh`)")
        return "receive rail: " + " · ".join(parts)
    except Exception:
        return ""


def x402_network() -> str:
    """The chain the per-request x402 rail settles on (``X402_DEFAULT_CHAIN``).

    Split out of :func:`get_x402_config` so a caller that only needs the CHAIN
    does not pay for ``resolve_treasury_address()``, which derives a wallet
    signing key. The public agent card and ``/api/x402/pricing`` both need the
    chain on every unauthenticated request (B41).
    """
    return (os.environ.get("X402_DEFAULT_CHAIN") or "base").strip() or "base"


def get_x402_config() -> Dict[str, Any]:
    """Get x402 configuration from environment.

    Returns:
        Configuration dict with:
        - pay_to: Treasury wallet address
        - network: Blockchain network
        - enabled: Whether x402 is enabled
    """
    return {
        "enabled": os.environ.get("X402_ENABLED", "false").lower() == "true",
        "pay_to": resolve_treasury_address(),
        "network": x402_network(),
        "cdp_key_id": os.environ.get("CDP_API_KEY_ID", ""),
        "cdp_key_secret": os.environ.get("CDP_API_KEY_SECRET", ""),
    }


def is_x402_properly_configured() -> bool:
    """Check if x402 is properly configured for mainnet payments.

    Returns:
        True if all required config is present
    """
    config = get_x402_config()

    if not config["enabled"]:
        return False

    if not config["pay_to"]:
        logger.warning("X402_PAYMENT_RECIPIENT not configured")
        return False

    # For mainnet, we need CDP credentials
    network = config["network"]
    if network in ["base", "avalanche", "iotex"]:  # Mainnets
        if not config["cdp_key_id"] or not config["cdp_key_secret"]:
            logger.warning(f"CDP credentials required for mainnet ({network})")
            return False

    return True
