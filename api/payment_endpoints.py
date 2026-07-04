"""Payment API endpoints for credit management and deposits."""

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import logging
from api.auth_constants import ADMIN_ROLES
from modules.credits.pricing import pricing, WELCOME_BONUS, DEN_SIGNUP_ALLOWANCE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


class DepositAddressResponse(BaseModel):
    """Deposit address response."""
    user_id: str
    deposit_address: str
    chains: List[str]
    qr_code_url: Optional[str] = None
    instructions: str


class CreditBalanceResponse(BaseModel):
    """Credit balance response."""
    user_id: str
    balance: int
    lifetime_earned: int
    lifetime_spent: int
    tier: str


class TransactionResponse(BaseModel):
    """Credit transaction response."""
    id: int
    user_id: str
    amount: int
    transaction_type: str
    reason: str
    session_id: Optional[str]
    balance_before: int
    balance_after: int
    timestamp: str


class PaginatedTransactionsResponse(BaseModel):
    """Paginated transactions response with metadata."""
    transactions: List[TransactionResponse]
    total: int
    limit: int
    offset: int
    has_more: bool


class CryptoPaymentResponse(BaseModel):
    """Crypto payment response."""
    id: int
    chain: str
    token_symbol: str
    amount: str
    amount_usd: float
    credits_purchased: int
    status: str
    detected_at: str


# Helper to get authenticated user from JWT — strict policy (rejects api_user / authenticated_api_user).
# Delegates to the canonical implementation in api.dependencies.
from api.dependencies import get_user_strict as get_authenticated_user


@router.get("/deposit-address", response_model=DepositAddressResponse)
async def get_deposit_address(
    request: Request,
    user_id: str = Depends(get_authenticated_user)
):
    """Get or create deposit address for user.

    Returns a unique deposit address for receiving crypto payments.
    Credits are automatically added when deposits are detected.
    """
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()

        wallet_gen = container.get_service('wallet_generator')
        db_manager = container.get_service('database_manager')
        
        # Check if user is admin - admins don't need deposit addresses
        role = getattr(request.state, 'role', 'user')
        is_admin = role in ADMIN_ROLES

        if not wallet_gen or not db_manager:
            # If services not available and user is admin, return placeholder
            if is_admin:
                logger.info(f"Wallet generator unavailable, returning placeholder for admin {user_id}")
                return {
                    "user_id": user_id,
                    "deposit_address": "N/A - Admin account has unlimited credits",
                    "chains": [],
                    "qr_code_url": None,
                    "instructions": "As an admin, you have unlimited credits and do not need to deposit funds."
                }
            raise HTTPException(
                status_code=503,
                detail="Payment services temporarily unavailable"
            )

        # Check if user already has deposit address
        existing = await db_manager.fetch_one("""
            SELECT deposit_address, generated_at
            FROM user_deposit_addresses
            WHERE user_id = ?
        """, (user_id,))

        if existing:
            deposit_address = existing['deposit_address']
            logger.info(f"Retrieved existing deposit address for user {user_id}")
        else:
            # Generate new deposit address
            deposit_address = wallet_gen.generate_deposit_address(user_id)

            # Store in database (user should exist with deterministic ID)
            try:
                await db_manager.execute("""
                    INSERT INTO user_deposit_addresses (
                        user_id, deposit_address, generated_at
                    ) VALUES (?, ?, datetime('now'))
                """, (user_id, deposit_address))
                logger.info(f"Generated new deposit address for user {user_id}: {deposit_address}")
            except Exception as e:
                # FK constraint - user doesn't exist, trigger re-auth
                logger.error(f"Failed to create deposit address - user {user_id} not found: {e}")
                raise HTTPException(
                    status_code=401,
                    detail="Session expired. Please sign in again with your wallet."
                )

        return DepositAddressResponse(
            user_id=user_id,
            deposit_address=deposit_address,
            chains=["ethereum", "sepolia"],
            qr_code_url=f"https://chart.googleapis.com/chart?chs=200x200&cht=qr&chl={deposit_address}",
            instructions=(
                f"Send USDC, USDT, or ETH to this address on Ethereum mainnet or Sepolia testnet. "
                f"Credits will be added automatically (1 credit = $0.01 USD). "
                f"Minimum deposit: $5 USD."
            )
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting deposit address for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to generate deposit address. Please try again later."
        )


@router.get("/balance")
async def get_credit_balance(
    request: Request,
    user_id: str = Depends(get_authenticated_user)
):
    """Get current credit balance and usage statistics.

    Shows:
    - Current balance
    - Lifetime earned/spent
    - Tier
    """
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()

        balance_mgr = container.get_service('balance_manager')
        tier_mgr = container.get_service('tier_manager')

        # Check if user is admin - admins get unlimited credits
        role = getattr(request.state, 'role', 'user')
        is_admin = role in ADMIN_ROLES

        if not balance_mgr:
            # If balance manager not available and user is admin, return unlimited balance
            if is_admin:
                logger.info(f"Balance manager unavailable, returning unlimited balance for admin {user_id}")
                return {
                    "user_id": user_id,
                    "balance": 999999,
                    "lifetime_earned": 999999,
                    "lifetime_spent": 0,
                    "tier": "admin"
                }
            raise HTTPException(
                status_code=503,
                detail="Balance service temporarily unavailable"
            )

        # Get balance
        balance_info = await balance_mgr.get_balance(user_id)

        # Get tier
        tier = "free"
        if tier_mgr:
            try:
                tier = await tier_mgr.get_user_tier(user_id)
            except HTTPException:
                pass

        return CreditBalanceResponse(
            user_id=user_id,
            balance=balance_info['balance'],
            lifetime_earned=balance_info['lifetime_earned'],
            lifetime_spent=balance_info['lifetime_spent'],
            tier=tier
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting balance for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve balance. Please try again later."
        )


@router.get("/transactions")
async def get_transactions(
    request: Request,
    user_id: str = Depends(get_authenticated_user),
    limit: int = 100,
    offset: int = 0,
    paginated: bool = False
):
    """Get credit transaction history.

    Shows all credit additions and deductions with reasons.

    Args:
        limit: Max transactions to return (default 100, max 500)
        offset: Skip first N transactions for pagination
        paginated: If true, returns {transactions, total, has_more} format
    """
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()

        db_manager = container.get_service('database_manager')

        # Check if user is admin
        role = getattr(request.state, 'role', 'user')
        is_admin = role in ADMIN_ROLES

        if not db_manager:
            # If database not available and user is admin, return empty
            if is_admin:
                logger.info(f"Database unavailable, returning empty transactions for admin {user_id}")
                if paginated:
                    return {"transactions": [], "total": 0, "limit": limit, "offset": offset, "has_more": False}
                return []
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")

        # Enforce max limit to prevent abuse
        limit = min(limit, 500)

        # Get total count for pagination
        total_result = await db_manager.fetch_one("""
            SELECT COUNT(*) as total FROM credit_transactions WHERE user_id = ?
        """, (user_id,))
        total = total_result['total'] if total_result else 0

        # Fetch transactions
        transactions = await db_manager.fetch_all("""
            SELECT
                id,
                user_id,
                amount,
                transaction_type,
                reason,
                session_id,
                balance_before,
                balance_after,
                timestamp
            FROM credit_transactions
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """, (user_id, limit, offset))

        tx_list = [
            TransactionResponse(
                id=tx['id'],
                user_id=tx['user_id'],
                amount=tx['amount'],
                transaction_type=tx['transaction_type'],
                reason=tx['reason'],
                session_id=tx['session_id'],
                balance_before=tx['balance_before'] or 0,
                balance_after=tx['balance_after'] or 0,
                timestamp=tx['timestamp']
            )
            for tx in transactions
        ]

        # Return paginated or simple list based on request
        if paginated:
            return PaginatedTransactionsResponse(
                transactions=tx_list,
                total=total,
                limit=limit,
                offset=offset,
                has_more=(offset + len(tx_list)) < total
            )

        return tx_list

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting transactions for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve transactions. Please try again later."
        )


@router.get("/deposits")
async def get_deposits(
    request: Request,
    user_id: str = Depends(get_authenticated_user),
    limit: int = 100,
    offset: int = 0
):
    """Get crypto deposit history.

    Shows all detected deposits from blockchain.

    Args:
        limit: Max deposits to return (default 100, max 500)
        offset: Skip first N deposits for pagination
    """
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()

        db_manager = container.get_service('database_manager')

        # Check if user is admin
        role = getattr(request.state, 'role', 'user')
        is_admin = role in ADMIN_ROLES

        if not db_manager:
            # If database not available and user is admin, return empty list
            if is_admin:
                logger.info(f"Database unavailable, returning empty deposits for admin {user_id}")
                return []
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")

        # Enforce max limit
        limit = min(limit, 500)

        # Fetch deposits
        deposits = await db_manager.fetch_all("""
            SELECT
                id,
                chain,
                token_symbol,
                amount,
                amount_usd,
                credits_purchased,
                status,
                detected_at
            FROM crypto_payments
            WHERE user_id = ?
            ORDER BY detected_at DESC
            LIMIT ? OFFSET ?
        """, (user_id, limit, offset))

        return [
            CryptoPaymentResponse(
                id=dep['id'],
                chain=dep['chain'],
                token_symbol=dep['token_symbol'],
                amount=dep['amount'],
                amount_usd=dep['amount_usd'],
                credits_purchased=dep['credits_purchased'],
                status=dep['status'],
                detected_at=dep['detected_at']
            )
            for dep in deposits
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting deposits for user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve deposits. Please try again later."
        )


@router.get("/pricing")
async def get_pricing():
    """Get credit pricing information.

    Public endpoint - no auth required.
    """
    return {
        "credit_rate": pricing.CREDIT_VALUE_USD,  # Dynamic from pricing config
        "minimum_deposit_usd": 5.00,
        "supported_tokens": [
            {
                "symbol": "USDC",
                "name": "USD Coin",
                "chains": ["ethereum", "sepolia"]
            },
            {
                "symbol": "USDT",
                "name": "Tether USD",
                "chains": ["ethereum", "sepolia"]
            },
            {
                "symbol": "ETH",
                "name": "Ethereum",
                "chains": ["ethereum", "sepolia"]
            }
        ],
        "tier_allowances": {
            "free": {
                "welcome_bonus": WELCOME_BONUS,
                "signup_allowance": 0,
                "requires_den_token": False
            },
            "holder": {
                "welcome_bonus": WELCOME_BONUS,
                "signup_allowance_per_token": DEN_SIGNUP_ALLOWANCE,
                "requires_den_token": True,
                "den_tokens_required": "1+"
            }
        }
    }
