"""Identity mapper for mapping wallets to internal user_ids."""

import hashlib
import logging
from datetime import datetime
from typing import Optional, List

# Import credit constants from single source of truth
from modules.credits.pricing import WELCOME_BONUS

logger = logging.getLogger(__name__)


class IdentityMapper:
    """Map wallet addresses to internal user_ids (deterministic)."""

    def __init__(self, db, user_profiles):
        """
        Initialize identity mapper.

        Args:
            db: Database manager instance
            user_profiles: UserProfiles instance
        """
        self.db = db
        self.user_profiles = user_profiles
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

        return user_id
