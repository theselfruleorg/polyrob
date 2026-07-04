"""Tier manager for managing user tiers based on NFT ownership."""

import logging

from core.exceptions import TierError, UserNotFoundError

# Import credit constants from single source of truth
from modules.credits.pricing import DEN_SIGNUP_ALLOWANCE

logger = logging.getLogger(__name__)


class TierManager:
    """Manage user tiers based on NFT ownership."""

    # Tier quotas (SIMPLIFIED: holder = 1+ DEN tokens)
    # Credits: 100 welcome (everyone) + 2000 DEN Sign Up Allowance per token
    TIER_LIMITS = {
        "holder": {
            "signup_allowance_per_token": DEN_SIGNUP_ALLOWANCE,  # $20 USD one-time per token ID
            "max_concurrent_sessions": 10,
            "max_steps": 50,
            "allowed_models": ["gpt-5", "claude-sonnet-4-5", "gemini-2.5-flash", "deepseek-chat"]
        },
        "admin": {
            "signup_allowance_per_token": 0,  # Admins don't need allowance
            "max_concurrent_sessions": 100,
            "max_steps": 500,
            "allowed_models": "*"  # All models
        }
    }

    def __init__(self, db, alchemy_tool=None):
        """
        Initialize tier manager.

        Args:
            db: Database manager instance
            alchemy_tool: AlchemyTool instance for NFT checks (optional)
        """
        self.db = db
        self.alchemy_tool = alchemy_tool
        self.logger = logging.getLogger('auth.tier_manager')

    async def get_user_tier(self, user_id: str) -> str:
        """Get user's current tier.

        UPDATED: Returns tier as-is (including 'free').
        Access control is enforced at feature level, not here.
        """

        result = await self.db.fetch_one("""
            SELECT tier FROM user_profiles WHERE user_id = ?
        """, (user_id,))

        tier = result['tier'] if result else 'free'

        return tier

    async def get_tier_limits(self, user_id: str) -> dict:
        """Get quota limits for user's tier.

        For free tier, returns minimal limits (info only - access blocked at feature level).
        Credits: 100 welcome (everyone) + 2000 DEN Sign Up Allowance per token.
        """

        tier = await self.get_user_tier(user_id)

        # Free tier gets minimal limits (shown in UI but features are blocked)
        if tier == 'free':
            return {
                "signup_allowance_per_token": 0,
                "max_concurrent_sessions": 0,
                "max_steps": 0,
                "allowed_models": []
            }

        # x402 and free_access tiers use holder limits
        # - x402: pay-per-request users
        # - free_access: admin-granted access without DEN token
        if tier in ('x402', 'free_access'):
            return self.TIER_LIMITS['holder']

        # Return actual limits for holder/admin
        if tier not in self.TIER_LIMITS:
            raise TierError("Invalid tier - contact support")

        return self.TIER_LIMITS[tier]

    async def check_quota(self, user_id: str, quota_type: str) -> bool:
        """Check if user has quota for resource."""

        limits = await self.get_tier_limits(user_id)

        if quota_type == "concurrent_sessions":
            # Count active sessions
            # Note: This assumes a sessions table exists
            # If not, this check will be skipped
            try:
                active = await self.db.fetch_one("""
                    SELECT COUNT(*) as count
                    FROM sessions
                    WHERE user_id = ? AND status IN ('running', 'paused')
                """, (user_id,))

                if active:
                    return active['count'] < limits['max_concurrent_sessions']
            except Exception as e:
                self.logger.warning(f"Could not check session quota: {e}")
                return True  # Allow if we can't check

        return True

    async def check_model_allowed(self, user_id: str, model: str) -> bool:
        """Check if user's tier allows this model."""

        limits = await self.get_tier_limits(user_id)
        allowed = limits['allowed_models']

        if allowed == "*":
            return True

        return model in allowed

    async def get_user_info(self, user_id: str) -> dict:
        """Get comprehensive user tier information."""

        result = await self.db.fetch_one("""
            SELECT
                u.tier,
                u.wallet_address,
                (u.den_token_count > 0) as has_den_token,
                u.den_token_verified_at,
                c.balance,
                c.lifetime_earned,
                c.lifetime_spent
            FROM user_profiles u
            LEFT JOIN user_credits c ON u.user_id = c.user_id
            WHERE u.user_id = ?
        """, (user_id,))

        if not result:
            raise UserNotFoundError(f"User not found: {user_id}")

        tier = result['tier']

        # Get limits - use get_tier_limits logic for consistency
        if tier == 'free':
            limits = {
                "signup_allowance_per_token": 0,
                "max_concurrent_sessions": 0,
                "max_steps": 0,
                "allowed_models": []
            }
        elif tier in ('x402', 'free_access'):
            # x402 and free_access use holder limits
            limits = self.TIER_LIMITS['holder']
        else:
            limits = self.TIER_LIMITS.get(tier, self.TIER_LIMITS.get('holder', {}))

        return {
            "user_id": user_id,
            "tier": tier,
            "wallet_address": result['wallet_address'],
            "has_den_token": bool(result['has_den_token']),
            "den_token_verified_at": result['den_token_verified_at'],
            "limits": limits,
            "credits": {
                "balance": result['balance'] or 0,
                "lifetime_earned": result['lifetime_earned'] or 0,
                "lifetime_spent": result['lifetime_spent'] or 0
            }
        }
