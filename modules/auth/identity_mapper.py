"""Identity mapper for mapping wallets to internal user_ids."""

import hashlib
import logging
from datetime import datetime
from typing import Optional, List

# Import credit constants from single source of truth
from modules.credits.pricing import WELCOME_BONUS
from core.token_check_hook import token_checker

logger = logging.getLogger(__name__)


class IdentityMapper:
    """Map wallet addresses to internal user_ids (deterministic)."""

    def __init__(self, db, user_profiles, alchemy_tool=None):
        """
        Initialize identity mapper.

        Args:
            db: Database manager instance
            user_profiles: UserProfiles instance
            alchemy_tool: AlchemyTool instance for NFT checks (optional)
        """
        self.db = db
        self.user_profiles = user_profiles
        self.alchemy_tool = alchemy_tool
        self.logger = logging.getLogger('auth.identity_mapper')
    
    def _generate_deterministic_user_id(self, wallet_address: str) -> str:
        """
        Generate deterministic user_id from wallet address.
        
        CRITICAL: Same wallet ALWAYS returns same user_id.
        This ensures stability across database deployments.
        
        Args:
            wallet_address: Ethereum wallet address (0x...)
            
        Returns:
            user_id: usr_<16 hex chars> (deterministic)
        """
        wallet_lower = wallet_address.lower().strip()
        hash_bytes = hashlib.sha256(wallet_lower.encode()).digest()
        return f"usr_{hash_bytes.hex()[:16]}"

    async def get_or_create_user(
        self,
        wallet_address: str,
        chain: str = 'ethereum',
        email: str = None
    ) -> str:
        """
        Get or create user by wallet address (WALLET-FIRST).

        Args:
            wallet_address: Ethereum wallet (PRIMARY identifier)
            chain: Blockchain network
            email: Optional email for notifications

        Returns:
            user_id: Internal database identifier
        """

        wallet_address = wallet_address.lower()

        # Check if wallet exists
        existing = await self.db.fetch_one("""
            SELECT user_id FROM user_profiles WHERE wallet_address = ?
        """, (wallet_address,))

        if existing:
            user_id = existing['user_id']
            self.logger.info(f"Found existing user for wallet {wallet_address[:10]}...")

            # Update last connection
            await self.db.execute("""
                UPDATE user_profiles
                SET current_wallet_chain = ?,
                    current_wallet_connected_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (chain, user_id))

            # Update tier (token count may have changed)
            await self._update_tier(user_id, wallet_address)

            return user_id

        # STEP 2: Check if email exists (ACCOUNT LINKING)
        if email:
            existing_email = await self.db.fetch_one("""
                SELECT user_id, wallet_address FROM user_profiles
                WHERE email = ?
            """, (email.lower(),))

            if existing_email:
                user_id = existing_email['user_id']

                # Link wallet to existing email account
                if not existing_email['wallet_address']:
                    await self.db.execute("""
                        UPDATE user_profiles
                        SET wallet_address = ?,
                            current_wallet_chain = ?,
                            current_wallet_connected_at = CURRENT_TIMESTAMP
                        WHERE user_id = ?
                    """, (wallet_address, chain, user_id))
                    self.logger.info(f"Linked wallet to existing email user: {user_id}")

                    # Update tier
                    await self._update_tier(user_id, wallet_address)

                return user_id

        # Create new user with DETERMINISTIC user_id
        user_id = self._generate_deterministic_user_id(wallet_address)
        
        self.logger.info(f"Creating new user with deterministic ID {user_id} for wallet {wallet_address[:10]}...")

        await self.db.execute("""
            INSERT INTO user_profiles (
                user_id,
                wallet_address,
                email,
                current_wallet_chain,
                role,
                tier,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, 'user', 'free', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (user_id, wallet_address, email, chain))

        # Initialize credits with welcome bonus (everyone gets this)
        await self.db.execute("""
            INSERT INTO user_credits (user_id, balance, lifetime_earned)
            VALUES (?, ?, ?)
        """, (user_id, WELCOME_BONUS, WELCOME_BONUS))

        # Record welcome bonus
        await self.db.execute("""
            INSERT INTO credit_transactions (
                user_id, amount, transaction_type, reason,
                balance_before, balance_after
            ) VALUES (?, ?, 'welcome', 'Sign Up Bonus', 0, ?)
        """, (user_id, WELCOME_BONUS, WELCOME_BONUS))

        self.logger.info(f"Created new user {user_id} for wallet {wallet_address[:10]}...")

        # Verify token ownership and set tier
        await self._update_tier(user_id, wallet_address)

        return user_id

    async def _update_tier(self, user_id: str, wallet_address: str):
        """
        Update user tier based on NFT ownership.

        BETA MODEL: Must have ≥1 token for access.

        IMPORTANT: Preserves admin-granted tiers (free_access, admin, x402).
        Only updates tier if user is currently 'free' or 'holder'.
        """

        check_token = token_checker()
        if not self.alchemy_tool or check_token is None:
            self.logger.warning("Alchemy tool not available - cannot verify token ownership")
            return

        try:
            # Check current tier - preserve admin-granted tiers
            current = await self.db.fetch_one("""
                SELECT tier FROM user_profiles WHERE user_id = ?
            """, (user_id,))

            current_tier = current['tier'] if current else 'free'

            # PRESERVE admin-granted tiers - don't overwrite them
            admin_granted_tiers = {'free_access', 'admin', 'x402'}
            if current_tier in admin_granted_tiers:
                self.logger.info(f"User {user_id} has admin-granted tier '{current_tier}' - preserving (not overwriting)")
                # Still update token count for reference, but don't change tier
                result = await check_token(self.alchemy_tool, wallet_address)
                token_count = result.get('token_count', 0)

                await self.db.execute("""
                    UPDATE user_profiles
                    SET den_token_count = ?, den_token_verified_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (token_count, user_id))
                return

            # Check NFT ownership via Alchemy tool
            result = await check_token(self.alchemy_tool, wallet_address)

            has_token = result.get('has_token', False)
            token_count = result.get('token_count', 0)

            # Determine tier based on token ownership
            # SIMPLIFIED: holder = 1+ tokens (no premium tier)
            if token_count == 0 or not has_token:
                tier = "free"
                self.logger.info(f"User {user_id} has no DEN tokens - tier set to 'free'")
            else:
                tier = "holder"

            # Update tier and token count
            await self.db.execute("""
                UPDATE user_profiles
                SET tier = ?, den_token_count = ?, den_token_verified_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (tier, token_count, user_id))

            self.logger.info(f"User {user_id} tier updated to: {tier} (tokens: {token_count})")

        except Exception as e:
            self.logger.error(f"Error updating tier for user {user_id}: {e}")
            raise
