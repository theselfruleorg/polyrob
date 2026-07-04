from typing import Optional, List
from modules.database.connection import DatabaseConnection
from modules.memory.models import UserProfile
import logging
import hashlib
from datetime import datetime


class UserProfiles:
    """Handles operations related to user_profiles table (wallet-based auth only)."""

    def __init__(self, db: DatabaseConnection):
        self.db = db
        self.logger = logging.getLogger('database.user_profiles')

    async def create_table(self) -> None:
        """Create the user_profiles table if it doesn't already exist."""
        try:
            await self.db.execute('''
                CREATE TABLE IF NOT EXISTS user_profiles (
                    -- IDENTIFIERS
                    user_id TEXT PRIMARY KEY,
                    wallet_address TEXT UNIQUE NOT NULL,

                    -- OPTIONAL PROFILE
                    email TEXT,
                    first_name TEXT,
                    last_name TEXT,

                    -- AUTHORIZATION
                    role TEXT DEFAULT 'user' NOT NULL,
                    tier TEXT DEFAULT 'free' NOT NULL,

                    -- WALLET TRACKING
                    current_wallet_chain TEXT DEFAULT 'ethereum',
                    current_wallet_connected_at TIMESTAMP,

                    -- TOKEN OWNERSHIP
                    den_token_count INTEGER DEFAULT 0,
                    den_token_verified_at TIMESTAMP,

                    -- METADATA
                    total_sessions INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    -- CONSTRAINTS (simplified: only user/admin roles)
                    CHECK (role IN ('user', 'admin')),
                    CHECK (tier IN ('free', 'free_access', 'holder', 'x402', 'admin'))
                )
            ''')

            # F1 (live-test): backfill columns missing from a pre-existing
            # older-schema user_profiles table. CREATE TABLE IF NOT EXISTS no-ops
            # on a stale table, so without this the indexes/queries below crash
            # (e.g. `no such column: tier` on a bot.db created before 'tier'
            # existed — which would also break a stale prod DB on deploy).
            # Idempotent: a fresh table already has every column, so nothing runs.
            try:
                existing_cols = {
                    r["name"]
                    for r in await self.db.fetch_all("PRAGMA table_info(user_profiles)")
                }
                backfill = [
                    ("email", "TEXT"),
                    ("tier", "TEXT NOT NULL DEFAULT 'free'"),
                    ("current_wallet_chain", "TEXT DEFAULT 'ethereum'"),
                    ("current_wallet_connected_at", "TIMESTAMP"),
                    ("den_token_count", "INTEGER DEFAULT 0"),
                    ("den_token_verified_at", "TIMESTAMP"),
                    ("total_sessions", "INTEGER DEFAULT 0"),
                ]
                for col, ddl in backfill:
                    if col not in existing_cols:
                        await self.db.execute(
                            f"ALTER TABLE user_profiles ADD COLUMN {col} {ddl}"
                        )
                        self.logger.info(
                            f"user_profiles: backfilled missing column '{col}' on a stale table"
                        )
            except Exception as e:
                self.logger.error(f"user_profiles column backfill failed: {e}", exc_info=True)
                raise

            # Create indexes
            await self.db.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet
                ON user_profiles(wallet_address)
            ''')
            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_role
                ON user_profiles(role)
            ''')
            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_tier
                ON user_profiles(tier)
            ''')
            await self.db.execute('''
                CREATE INDEX IF NOT EXISTS idx_email
                ON user_profiles(email) WHERE email IS NOT NULL
            ''')

            self.logger.info("📊 User profiles table and indices verified/created")
        except Exception as e:
            self.logger.error(f"❌ Error creating user_profiles table: {str(e)}", exc_info=True)
            raise

    def generate_user_id_from_wallet(self, wallet_address: str) -> str:
        """Generate a deterministic user ID from wallet address.

        IMPORTANT: Must match IdentityMapper._generate_deterministic_user_id()
        Format: usr_<16 hex chars>

        Args:
            wallet_address: Ethereum wallet address (0x...)

        Returns:
            str: Deterministic user ID in format 'usr_<16 hex chars>'
        """
        # Normalize wallet address (lowercase, stripped)
        wallet_lower = wallet_address.lower().strip()

        # Create deterministic hash - MUST match identity_mapper.py
        hash_bytes = hashlib.sha256(wallet_lower.encode()).digest()
        return f"usr_{hash_bytes.hex()[:16]}"

    async def get_user_by_email(self, email: str) -> Optional[UserProfile]:
        """Retrieve a user profile by email address."""
        try:
            row = await self.db.fetch_one(
                "SELECT * FROM user_profiles WHERE email = ?",
                (email,)
            )
            if row:
                self.logger.debug(f"🔍 Retrieved profile for email {email}")
                return self._row_to_user_profile(row)
            self.logger.debug(f"🔍 No profile found for email {email}")
            return None
        except Exception as e:
            self.logger.error(f"❌ Error retrieving user profile for email {email}: {str(e)}", exc_info=True)
            raise

    async def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        """Retrieve a user profile by user ID (hash)."""
        try:
            row = await self.db.fetch_one(
                "SELECT * FROM user_profiles WHERE user_id = ?",
                (user_id,)
            )
            if row:
                self.logger.debug(f"🔍 Retrieved profile for user {user_id}")
                return self._row_to_user_profile(row)
            self.logger.debug(f"🔍 No profile found for user {user_id}")
            return None
        except Exception as e:
            self.logger.error(f"❌ Error retrieving user profile for {user_id}: {str(e)}", exc_info=True)
            raise

    async def get_user_by_wallet_address(self, wallet_address: str) -> Optional[UserProfile]:
        """Retrieve a user profile by wallet address."""
        try:
            row = await self.db.fetch_one(
                "SELECT * FROM user_profiles WHERE wallet_address = ?",
                (wallet_address.lower(),)
            )
            if row:
                self.logger.debug(f"🔍 Retrieved profile for wallet {wallet_address}")
                return self._row_to_user_profile(row)
            self.logger.debug(f"🔍 No profile found for wallet {wallet_address}")
            return None
        except Exception as e:
            self.logger.error(f"❌ Error retrieving user profile for wallet {wallet_address}: {str(e)}", exc_info=True)
            raise
            
    def _row_to_user_profile(self, row) -> UserProfile:
        """Convert database row to UserProfile object."""
        # Parse den_token_verified_at timestamp if present
        den_token_verified_at = None
        if row.get('den_token_verified_at'):
            try:
                if isinstance(row['den_token_verified_at'], str):
                    den_token_verified_at = datetime.fromisoformat(row['den_token_verified_at'])
                else:
                    den_token_verified_at = row['den_token_verified_at']
            except (ValueError, TypeError) as e:
                self.logger.debug(f"Failed to parse den_token_verified_at: {row.get('den_token_verified_at')} - {e}")
                den_token_verified_at = None
        
        # Parse wallet_connected_at timestamp if present
        current_wallet_connected_at = None
        if row.get('current_wallet_connected_at'):
            try:
                if isinstance(row['current_wallet_connected_at'], str):
                    current_wallet_connected_at = datetime.fromisoformat(row['current_wallet_connected_at'])
                else:
                    current_wallet_connected_at = row['current_wallet_connected_at']
            except (ValueError, TypeError) as e:
                self.logger.debug(f"Failed to parse current_wallet_connected_at: {row.get('current_wallet_connected_at')} - {e}")
                current_wallet_connected_at = None
        
        # Parse created_at/updated_at timestamps
        created_at = None
        if row.get('created_at'):
            try:
                if isinstance(row['created_at'], str):
                    created_at = datetime.fromisoformat(row['created_at'])
                else:
                    created_at = row['created_at']
            except (ValueError, TypeError) as e:
                self.logger.debug(f"Failed to parse created_at: {row.get('created_at')} - {e}")
                created_at = None
                
        updated_at = None
        if row.get('updated_at'):
            try:
                if isinstance(row['updated_at'], str):
                    updated_at = datetime.fromisoformat(row['updated_at'])
                else:
                    updated_at = row['updated_at']
            except (ValueError, TypeError) as e:
                self.logger.debug(f"Failed to parse updated_at: {row.get('updated_at')} - {e}")
                updated_at = None
        
        user_profile = UserProfile(
            user_id=row['user_id'],
            wallet_address=row['wallet_address'],
            email=row.get('email'),
            first_name=row.get('first_name'),
            last_name=row.get('last_name'),
            role=row.get('role', 'user'),
            tier=row.get('tier', 'free'),
            current_wallet_chain=row.get('current_wallet_chain', 'ethereum'),
            current_wallet_connected_at=current_wallet_connected_at,
            den_token_count=row.get('den_token_count', 0),
            den_token_verified_at=den_token_verified_at,
            total_sessions=row.get('total_sessions', 0),
            created_at=created_at,
            updated_at=updated_at
        )
        return user_profile

    async def upsert_user_profile(self, user_profile: UserProfile) -> None:
        """Insert or update a user profile."""
        try:
            await self.db.execute("""
                INSERT INTO user_profiles (
                    user_id, wallet_address, email, first_name, last_name,
                    role, tier, current_wallet_chain, current_wallet_connected_at,
                    den_token_count, den_token_verified_at, total_sessions,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    wallet_address=excluded.wallet_address,
                    email=excluded.email,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    role=COALESCE(excluded.role, user_profiles.role),
                    tier=excluded.tier,
                    current_wallet_chain=excluded.current_wallet_chain,
                    current_wallet_connected_at=excluded.current_wallet_connected_at,
                    den_token_count=excluded.den_token_count,
                    den_token_verified_at=excluded.den_token_verified_at,
                    total_sessions=excluded.total_sessions,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                user_profile.user_id,
                user_profile.wallet_address,
                getattr(user_profile, 'email', None),
                getattr(user_profile, 'first_name', None),
                getattr(user_profile, 'last_name', None),
                getattr(user_profile, 'role', 'user'),
                getattr(user_profile, 'tier', 'free'),
                getattr(user_profile, 'current_wallet_chain', 'ethereum'),
                getattr(user_profile, 'current_wallet_connected_at', None),
                getattr(user_profile, 'den_token_count', 0),
                getattr(user_profile, 'den_token_verified_at', None),
                getattr(user_profile, 'total_sessions', 0)
            ))
            self.logger.info(f"Upserted profile for user_id: {user_profile.user_id}")
        except Exception as e:
            self.logger.error(f"Failed to upsert user profile for {user_profile.user_id}: {e}")
            raise

    async def get_or_create_by_wallet(self, wallet_address: str, user_data: dict = None) -> UserProfile:
        """Get or create a user profile by wallet address.
        
        Args:
            wallet_address: Ethereum wallet address
            user_data: Additional user data for new profiles
            
        Returns:
            UserProfile: Existing or newly created profile
        """
        try:
            # Validate wallet address format
            if not wallet_address or not wallet_address.startswith('0x') or len(wallet_address) != 42:
                raise ValueError(f"Invalid wallet address format: {wallet_address}")
            
            # Normalize wallet address
            normalized_wallet = wallet_address.lower()
            
            # Try to get existing user
            user = await self.get_user_by_wallet_address(normalized_wallet)
            if user:
                return user
                
            # Create new user with deterministic ID from wallet
            user_id = self.generate_user_id_from_wallet(normalized_wallet)
            
            # Default user data
            data = {
                'first_name': '',
                'last_name': '',
                'email': None,
            }
            
            # Update with provided data
            if user_data:
                data.update(user_data)
            
            # Create profile
            profile = UserProfile(
                user_id=user_id,
                wallet_address=normalized_wallet,
                first_name=data.get('first_name', ''),
                last_name=data.get('last_name', ''),
                email=data.get('email'),
                role='user',
                tier='free',
                total_sessions=1
            )
            
            # Save to database
            await self.upsert_user_profile(profile)
            self.logger.info(f"Created new user profile for wallet {wallet_address}")
            
            return profile
            
        except Exception as e:
            self.logger.error(f"Failed to get or create user by wallet {wallet_address}: {e}")
            raise

    async def clear_all_user_profiles(self) -> None:
        """Delete all user profiles from the database."""
        try:
            await self.db.execute("DELETE FROM user_profiles")
            self.logger.debug("Cleared all user profiles from the database.")
        except Exception as e:
            self.logger.error(f"Failed to clear all user profiles: {e}")
            raise 

    async def is_wallet_existing(self, wallet_address: str) -> bool:
        """Check if a wallet address is already registered."""
        try:
            row = await self.db.fetch_one("""
                SELECT 1 FROM user_profiles WHERE wallet_address = ?
            """, (wallet_address.lower(),))
            return row is not None
        except Exception as e:
            self.logger.error(f"Error checking wallet existence for address {wallet_address}: {e}")
            raise 

    async def get_all(self) -> List[UserProfile]:
        """Get all user profiles."""
        try:
            query = """
                SELECT 
                    user_id,
                    wallet_address,
                    email,
                    first_name,
                    last_name,
                    role,
                    tier,
                    current_wallet_chain,
                    current_wallet_connected_at,
                    den_token_count,
                    den_token_verified_at,
                    total_sessions,
                    created_at,
                    updated_at
                FROM user_profiles
            """
            rows = await self.db.fetch_all(query)
            profiles = []
            for row in rows:
                try:
                    profile = self._row_to_user_profile(row)
                    profiles.append(profile)
                except Exception as e:
                    self.logger.error(f"Error processing user profile row: {e}")
                    self.logger.debug(f"Problematic row: {row}")
                    continue
                
            return profiles
        except Exception as e:
            self.logger.error(f"Error getting all user profiles: {e}")
            raise

    async def set_role(self, user_id: str, role: str) -> bool:
        """Set a user's role.

        Args:
            user_id: User ID to set role for
            role: Role to set ('user', 'admin')

        Returns:
            bool: True if role was set successfully
        """
        try:
            # Validate role using core constants as single source of truth
            from core.constants import VALID_ROLES
            if role not in VALID_ROLES:
                self.logger.error(f"Invalid role: {role}. Must be one of: {VALID_ROLES}")
                return False
                
            # Check if user exists
            user_exists = await self.db.fetch_one(
                "SELECT 1 FROM user_profiles WHERE user_id = ?",
                (user_id,)
            )
            
            if user_exists:
                # Update existing user
                await self.db.execute(
                    "UPDATE user_profiles SET role = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (role, user_id)
                )
                return True
            else:
                self.logger.error(f"User {user_id} not found when setting role")
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to set role for user {user_id}: {e}")
            return False
            
    async def get_admin_wallet_addresses(self) -> List[str]:
        """Get all wallet addresses with admin role.

        Returns:
            List[str]: List of wallet addresses with admin privileges
        """
        try:
            rows = await self.db.fetch_all(
                "SELECT wallet_address FROM user_profiles WHERE role = 'admin'"
            )
            return [row['wallet_address'] for row in rows]
            
        except Exception as e:
            self.logger.error(f"Failed to get admin wallet addresses: {e}")
            return []
            
    async def get_role(self, user_id: str) -> str:
        """Get a user's role.
        
        Args:
            user_id: User ID to get role for
            
        Returns:
            str: User's role, defaults to 'user' if not found
        """
        try:
            row = await self.db.fetch_one(
                "SELECT role FROM user_profiles WHERE user_id = ?",
                (user_id,)
            )
            
            if row and row['role']:
                return row['role']
            
            return 'user'  # Default role
            
        except Exception as e:
            self.logger.error(f"Failed to get role for user {user_id}: {e}")
            return 'user'

    async def increment_user_sessions(self, user_id: str) -> bool:
        """Increment total sessions for a user.
        
        Args:
            user_id: User ID to increment sessions for
            
        Returns:
            bool: True if sessions were incremented successfully
        """
        try:
            # Check if user exists
            user_exists = await self.db.fetch_one(
                "SELECT 1 FROM user_profiles WHERE user_id = ?",
                (user_id,)
            )
            
            if user_exists:
                # Increment sessions counter
                await self.db.execute(
                    "UPDATE user_profiles SET total_sessions = total_sessions + 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (user_id,)
                )
                self.logger.info(f"Incremented sessions counter for user {user_id}")
                return True
            else:
                self.logger.warning(f"User {user_id} not found when incrementing sessions")
                return False
            
        except Exception as e:
            self.logger.error(f"Failed to increment sessions for user {user_id}: {e}")
            return False

    async def set_tier(self, user_id: str, tier: str) -> bool:
        """Set a user's tier.

        Args:
            user_id: User ID to set tier for
            tier: Tier to set ('free', 'holder', 'x402', 'admin')

        Returns:
            bool: True if tier was set successfully
        """
        try:
            # Validate tier using core constants as single source of truth
            from core.constants import VALID_TIERS
            if tier not in VALID_TIERS:
                self.logger.error(f"Invalid tier: {tier}. Must be one of: {VALID_TIERS}")
                return False
                
            # Update user tier
            await self.db.execute(
                "UPDATE user_profiles SET tier = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (tier, user_id)
            )
            return True
                
        except Exception as e:
            self.logger.error(f"Failed to set tier for user {user_id}: {e}")
            return False

    async def update_token_count(self, user_id: str, token_count: int) -> bool:
        """Update user's DEN token count.
        
        Args:
            user_id: User ID to update
            token_count: New token count
            
        Returns:
            bool: True if updated successfully
        """
        try:
            await self.db.execute(
                """
                UPDATE user_profiles 
                SET den_token_count = ?, 
                    den_token_verified_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP 
                WHERE user_id = ?
                """,
                (token_count, user_id)
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to update token count for user {user_id}: {e}")
            return False
