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
) -> bool:
    """Record a settled x402 payment in our database.

    N1 fix: the previous INSERT omitted four NOT NULL columns
    (``amount``, ``recipient``, ``nonce``, ``deadline``), so every insert raised
    a constraint violation that was swallowed -> the agent settled USDC on-chain
    and persisted nothing. This now supplies every NOT NULL column and is
    idempotent on the unique ``nonce`` (a replayed on-chain tx is a no-op).

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

    Returns:
        True if recorded (or already recorded), False on error.
    """
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()
        db = container.get_service('database_manager')

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

        # Bare ON CONFLICT DO NOTHING makes a replayed PK/nonce a no-op while a
        # NOT NULL violation still RAISES (so a future missing-column bug is loud,
        # not silently swallowed like N1).
        await db.execute("""
            INSERT INTO x402_payment_requests (
                id, user_id, payer_address, amount, amount_usd, asset, chain,
                recipient, nonce, deadline, status, transaction_hash, payment_id,
                created_at, completed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      datetime('now'), datetime('now'), datetime('now'))
            ON CONFLICT DO NOTHING
        """, (
            payment_id,
            user_id,
            wallet_address.lower(),
            resolved_amount,
            amount_usd,
            asset,
            network,
            recipient.lower(),
            resolved_nonce,
            resolved_deadline,
            status,
            transaction_hash,
            payment_id,
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


def should_refund_on_status(status_code: int) -> bool:
    """Whether a downstream response status means the paid request failed.

    x402 settles BEFORE the downstream handler runs, so a server error (5xx)
    means the customer paid but got nothing -> flag the payment for refund.
    Client errors (4xx) are the caller's fault and are not refundable here.
    """
    return int(status_code) >= 500


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
        return True
    except Exception as e:
        logger.error(f"Failed to flag x402 refund_due for {payment_id}: {e}")
        return False


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
        "pay_to": os.environ.get("X402_PAYMENT_RECIPIENT", ""),
        "network": os.environ.get("X402_DEFAULT_CHAIN", "base"),
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
