"""Identity mapper for mapping wallets to internal user_ids."""

import hashlib
import logging
from datetime import datetime
from typing import Optional, List

# Import credit constants from single source of truth
from modules.credits.pricing import WELCOME_BONUS, DEN_SIGNUP_ALLOWANCE

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

    async def _update_wallet(self, user_id: str, wallet_address: str, chain: str = 'ethereum'):
        """Update user's current wallet and track chain."""

        # Get current wallet
        current = await self.db.fetch_one("""
            SELECT wallet_address, current_wallet_chain FROM user_profiles WHERE user_id = ?
        """, (user_id,))

        current_wallet = current['wallet_address'] if current else None
        current_chain = current['current_wallet_chain'] if current else None

        # If same wallet and chain, do nothing
        if current_wallet and current_wallet.lower() == wallet_address.lower() and current_chain == chain:
            return

        # Archive old wallet to history
        if current_wallet:
            await self.db.execute("""
                INSERT INTO wallet_history (
                    user_id, wallet_address, chain,
                    connected_at, disconnected_at
                )
                SELECT user_id, wallet_address, COALESCE(current_wallet_chain, 'ethereum'),
                       current_wallet_connected_at, CURRENT_TIMESTAMP
                FROM user_profiles WHERE user_id = ?
            """, (user_id,))

        # Update to new wallet with chain tracking
        await self.db.execute("""
            UPDATE user_profiles
            SET wallet_address = ?,
                current_wallet_chain = ?,
                current_wallet_connected_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (wallet_address, chain, user_id))

        # Update tier based on new wallet
        await self._update_tier(user_id, wallet_address)

        self.logger.info(f"User {user_id} switched wallet to {wallet_address[:8]}... ({chain})")

    async def _update_tier(self, user_id: str, wallet_address: str):
        """
        Update user tier based on NFT ownership.

        BETA MODEL: Must have ≥1 token for access.
        Grants ONE-TIME bonus per token ID (prevents multi-wallet abuse).

        IMPORTANT: Preserves admin-granted tiers (free_access, admin, x402).
        Only updates tier if user is currently 'free' or 'holder'.
        """

        if not self.alchemy_tool:
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
                from tools.alchemy.alchemy_tool import CheckTokenParams
                params = CheckTokenParams(address=wallet_address)
                result = await self.alchemy_tool.alchemy_check_token(params)
                token_count = result.get('token_count', 0)

                await self.db.execute("""
                    UPDATE user_profiles
                    SET den_token_count = ?, den_token_verified_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (token_count, user_id))
                return

            # Check NFT ownership via Alchemy tool
            from tools.alchemy.alchemy_tool import CheckTokenParams
            params = CheckTokenParams(address=wallet_address)

            result = await self.alchemy_tool.alchemy_check_token(params)

            has_token = result.get('has_token', False)
            token_count = result.get('token_count', 0)
            token_ids = result.get('token_ids', [])
            contract_address = result.get('contract_address', '')

            # Determine tier based on token ownership
            # SIMPLIFIED: holder = 1+ tokens (no premium tier)
            if token_count == 0 or not has_token:
                tier = "free"
                self.logger.info(f"User {user_id} has no DEN tokens - tier set to 'free'")
            else:
                tier = "holder"
                # Grant ONE-TIME Sign Up Allowance for each unused token ID
                if token_ids and contract_address:
                    allowance_granted = await self._grant_den_signup_allowance(
                        user_id=user_id,
                        token_ids=token_ids,
                        contract_address=contract_address
                    )
                    if allowance_granted > 0:
                        self.logger.info(f"Granted {allowance_granted} DEN Sign Up Allowance to {user_id}")

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

    async def _grant_den_signup_allowance(
        self,
        user_id: str,
        token_ids: List[str],
        contract_address: str
    ) -> int:
        """
        Grant ONE-TIME Sign Up Allowance for each unused DEN token ID.

        Each token ID can only grant allowance ONCE.
        We track by TOKEN ID only - not by user.

        NOTE: This is SEPARATE from access. User gets "holder" tier
        based on current token ownership, regardless of whether
        the allowance was already claimed by someone else.

        Args:
            user_id: User to grant credits to
            token_ids: List of token IDs currently owned
            contract_address: NFT contract address

        Returns:
            int: Total credits granted
        """
        total_granted = 0
        contract_address = contract_address.lower()

        for token_id in token_ids:
            # Normalize token_id to string
            token_id_str = str(token_id).strip()
            if not token_id_str:
                self.logger.warning(f"Skipping empty token_id")
                continue

            # Check if this token ID already used for allowance
            existing = await self.db.fetch_one("""
                SELECT 1 FROM den_token_bonuses
                WHERE token_id = ? AND contract_address = ?
            """, (token_id_str, contract_address))

            if existing:
                # Token already used - skip (but user still has access!)
                self.logger.debug(f"Token #{token_id_str} allowance already granted - skipping")
                continue

            # Token is fresh - grant allowance
            try:
                # Mark token as used FIRST (prevents race condition on token claim)
                await self.db.execute("""
                    INSERT INTO den_token_bonuses (token_id, contract_address)
                    VALUES (?, ?)
                """, (token_id_str, contract_address))

                # ATOMIC balance update to prevent race condition
                # Uses UPDATE with arithmetic instead of read-then-write
                # Also handles case where user_credits record might not exist
                update_result = await self.db.execute("""
                    UPDATE user_credits
                    SET balance = balance + ?,
                        lifetime_earned = lifetime_earned + ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (DEN_SIGNUP_ALLOWANCE, DEN_SIGNUP_ALLOWANCE, user_id))

                # Get updated balance for transaction record
                balance_result = await self.db.fetch_one("""
                    SELECT balance FROM user_credits WHERE user_id = ?
                """, (user_id,))

                if balance_result:
                    new_balance = balance_result['balance']
                    balance_before = new_balance - DEN_SIGNUP_ALLOWANCE

                    # Record transaction
                    await self.db.execute("""
                        INSERT INTO credit_transactions (
                            user_id, amount, transaction_type, reason,
                            balance_before, balance_after, timestamp
                        ) VALUES (?, ?, 'den_allowance', ?, ?, ?, CURRENT_TIMESTAMP)
                    """, (user_id, DEN_SIGNUP_ALLOWANCE, f"DEN Sign Up Allowance - Token #{token_id_str}",
                          balance_before, new_balance))

                    total_granted += DEN_SIGNUP_ALLOWANCE
                    self.logger.info(f"Granted {DEN_SIGNUP_ALLOWANCE} Sign Up Allowance for token #{token_id_str}")
                else:
                    # user_credits record doesn't exist - this shouldn't happen
                    # as welcome bonus creates it, but handle gracefully
                    self.logger.error(f"No user_credits record for {user_id} - cannot grant allowance")
                    # Rollback token claim
                    await self.db.execute("""
                        DELETE FROM den_token_bonuses
                        WHERE token_id = ? AND contract_address = ?
                    """, (token_id_str, contract_address))

            except Exception as e:
                # Primary key violation = token already used (race condition)
                if "UNIQUE constraint" in str(e) or "PRIMARY KEY" in str(e):
                    self.logger.debug(f"Token #{token_id_str} already claimed by another request")
                else:
                    self.logger.error(f"Error granting allowance for token #{token_id_str}: {e}")
                continue

        return total_granted
