"""Tier manager: the session limits of a user's account tier."""

import logging

from core.exceptions import TierError, UserNotFoundError

logger = logging.getLogger(__name__)


class TierManager:
    """Read a user's tier and the limits it carries."""

    # free_access (admin-granted) and x402 (pay-per-request) share the
    # standard limits; free gets none (access is blocked at feature level).
    STANDARD_LIMITS = {
        "max_concurrent_sessions": 10,
        "allowed_models": ["gpt-5", "claude-sonnet-4-5", "gemini-2.5-flash", "deepseek-chat"]
    }
    TIER_LIMITS = {
        "free": {"max_concurrent_sessions": 0, "allowed_models": []},
        "free_access": STANDARD_LIMITS,
        "x402": STANDARD_LIMITS,
        "admin": {
            "max_concurrent_sessions": 100,
            "allowed_models": "*"  # All models
        }
    }

    def __init__(self, db):
        """
        Initialize tier manager.

        Args:
            db: Database manager instance
        """
        self.db = db
        self.logger = logging.getLogger('auth.tier_manager')

    async def get_user_tier(self, user_id: str) -> str:
        """Get user's current tier (including 'free').

        Access control is enforced at feature level, not here.
        """

        result = await self.db.fetch_one("""
            SELECT tier FROM user_profiles WHERE user_id = ?
        """, (user_id,))

        return result['tier'] if result else 'free'

    def _limits_for(self, tier: str) -> dict:
        if tier not in self.TIER_LIMITS:
            raise TierError("Invalid tier - contact support")
        return self.TIER_LIMITS[tier]

    async def get_tier_limits(self, user_id: str) -> dict:
        """Get quota limits for user's tier."""

        return self._limits_for(await self.get_user_tier(user_id))

    async def get_user_info(self, user_id: str) -> dict:
        """Get comprehensive user tier information."""

        result = await self.db.fetch_one("""
            SELECT
                u.tier,
                u.wallet_address,
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

        return {
            "user_id": user_id,
            "tier": tier,
            "wallet_address": result['wallet_address'],
            "limits": self._limits_for(tier),
            "credits": {
                "balance": result['balance'] or 0,
                "lifetime_earned": result['lifetime_earned'] or 0,
                "lifetime_spent": result['lifetime_spent'] or 0
            }
        }
