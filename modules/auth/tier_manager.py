"""Tier manager for managing user tiers based on NFT ownership."""

import logging
import asyncio
from datetime import datetime, timezone

from core.exceptions import TierError, UserNotFoundError

# Import credit constants from single source of truth
from modules.credits.pricing import DEN_SIGNUP_ALLOWANCE

logger = logging.getLogger(__name__)


class TierManager:
    """Manage user tiers based on NFT ownership."""

    # Tier quotas (SIMPLIFIED: holder = 1+ DEN tokens)
    # Credits: optional welcome grant + 2000 DEN Sign Up Allowance per token
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
            SELECT tier, wallet_address, den_token_verified_at FROM user_profiles WHERE user_id = ?
        """, (user_id,))

        tier = result['tier'] if result else 'free'
        if tier == 'holder':
            tier = await self._refresh_holder(user_id, result)
        return tier

    async def _refresh_holder(self, user_id: str, row) -> str:
        """A transferred DEN cannot leave a permanent entitlement behind."""
        verified = row.get('den_token_verified_at')
        try:
            when = datetime.fromisoformat(str(verified).replace('Z', '+00:00'))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - when).total_seconds()
            if 0 <= age < 300:
                return 'holder'
        except (ValueError, TypeError):
            pass
        from core.token_check_hook import token_checker
        checker = token_checker()
        wallet = row.get('wallet_address')
        if checker is None or self.alchemy_tool is None or not wallet:
            raise TierError('DEN ownership verification is unavailable')
        try:
            result = await asyncio.wait_for(checker(self.alchemy_tool, wallet), timeout=10)
            count = result.get('token_count') if isinstance(result, dict) else None
            if (not isinstance(result, dict) or result.get('status') != 'success'
                    or type(count) is not int or count < 0
                    or type(result.get('has_token')) is not bool):
                raise ValueError('unverified ownership result')
        except Exception as exc:
            raise TierError('DEN ownership verification is unavailable') from exc
        tier = 'holder' if result['has_token'] and count > 0 else 'free'
        await self.db.execute(
            "UPDATE user_profiles SET tier=?, den_token_count=?, "
            "den_token_verified_at=CURRENT_TIMESTAMP WHERE user_id=? "
            "AND tier='holder' AND wallet_address=?", (tier, count, user_id, wallet))
        current = await self.db.fetch_one('SELECT tier FROM user_profiles WHERE user_id=?', (user_id,))
        return current['tier'] if current else 'free'

    async def get_tier_limits(self, user_id: str) -> dict:
        """Get quota limits for user's tier.

        For free tier, returns minimal limits (info only - access blocked at feature level).
        Credits: optional welcome grant + 2000 DEN Sign Up Allowance per token.
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
