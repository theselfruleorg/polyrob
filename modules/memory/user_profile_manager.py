"""User profile management - wallet-based authentication."""

from typing import Optional, Dict, Any, List
from datetime import datetime
import hashlib

from core.base_component import BaseComponent
from core.config import BotConfig
from core.exceptions import ComponentError
from modules.memory.models import UserProfile


class UserProfileManager(BaseComponent):
    """Manages user profiles with wallet-based authentication."""

    def __init__(
        self,
        name: str,
        config: BotConfig,
        database=None,
        cache=None
    ):
        """Initialize user profile manager."""
        super().__init__(name=name, config=config)
        self.database = database
        self.cache = cache
        self._profiles = {}
        self._admin_wallets_cache = None
        self._initialized = False
        self.user_roles = {}

    async def _initialize(self) -> None:
        """Initialize user profile manager."""
        try:
            self.logger.info("Starting User Profile Manager initialization")
            
            if not self.database:
                raise ComponentError("Database not provided")
            
            # Load existing profiles
            try:
                profiles = await self.database.user_profiles.get_all()
                for profile in profiles:
                    self._profiles[profile.user_id] = profile
                    
                    if hasattr(profile, 'role') and profile.role:
                        self.user_roles[profile.user_id] = profile.role
                        
                self.logger.info(f"Loaded {len(profiles)} user profiles")
            except Exception as e:
                self.logger.warning(f"Failed to load user profiles: {e}")
            
            self._initialized = True
            self.logger.info("User Profile Manager initialization completed")
            
        except Exception as e:
            self._initialized = False
            self.logger.error(f"User Profile Manager initialization failed: {e}")
            raise ComponentError(f"Failed to initialize user profile manager: {e}")

    async def _cleanup(self) -> None:
        """Clean up user profile manager resources."""
        try:
            self.logger.info("Starting User Profile Manager cleanup")
            
            for profile in self._profiles.values():
                try:
                    await self.save_user_profile(profile)
                except Exception as e:
                    self.logger.error(f"Failed to save profile {profile.user_id}: {e}")
            
            self._profiles.clear()
            self._admin_wallets_cache = None
            self.user_roles.clear()
            self._initialized = False
            self.logger.info("User Profile Manager cleanup completed")
            
        except Exception as e:
            self.logger.error(f"User Profile Manager cleanup failed: {e}")
            raise ComponentError(f"Failed to clean up user profile manager: {e}")

    def generate_user_id_from_wallet(self, wallet_address: str) -> str:
        """Generate a deterministic user ID from wallet address.
        
        Args:
            wallet_address: Ethereum wallet address (0x...)
            
        Returns:
            str: Hash-based user ID
        """
        normalized = wallet_address.lower()
        hash_obj = hashlib.sha256(normalized.encode())
        return hash_obj.hexdigest()[:24]

    async def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        """Get a user profile by ID.
        
        Args:
            user_id: Universal user ID (hash)
            
        Returns:
            Optional[UserProfile]: User profile if found
        """
        if not self._initialized:
            await self.initialize()

        # Try cache first
        if self.cache:
            cached_profile = await self.cache.get(f"user_profile:{user_id}")
            if cached_profile:
                return cached_profile

        # Get from database
        profile = await self.database.user_profiles.get_user_profile(user_id)
        
        if profile and self.cache:
            await self.cache.set(f"user_profile:{user_id}", profile)
            
        return profile
        
    async def get_user_by_email(self, email: str) -> Optional[UserProfile]:
        """Get a user profile by email address.
        
        Args:
            email: User's email address
            
        Returns:
            Optional[UserProfile]: User profile if found
        """
        if not self._initialized:
            await self.initialize()
            
        if self.cache:
            cached_profile = await self.cache.get(f"email_user:{email}")
            if cached_profile:
                return cached_profile
                
        profile = await self.database.user_profiles.get_user_by_email(email)
        
        if profile and self.cache:
            await self.cache.set(f"user_profile:{profile.user_id}", profile)
            await self.cache.set(f"email_user:{email}", profile)
            
        return profile

    async def get_user_by_wallet_address(self, wallet_address: str) -> Optional[UserProfile]:
        """Get a user profile by wallet address.
        
        Args:
            wallet_address: User's wallet address
            
        Returns:
            Optional[UserProfile]: User profile if found
        """
        if not self._initialized:
            await self.initialize()
            
        normalized = wallet_address.lower()
        
        if self.cache:
            cached_profile = await self.cache.get(f"wallet_user:{normalized}")
            if cached_profile:
                return cached_profile
                
        profile = await self.database.user_profiles.get_user_by_wallet_address(normalized)
        
        if profile and self.cache:
            await self.cache.set(f"user_profile:{profile.user_id}", profile)
            await self.cache.set(f"wallet_user:{normalized}", profile)
            
        return profile

    async def get_or_create_by_wallet(self, wallet_address: str, user_data: Dict[str, Any] = None) -> UserProfile:
        """Get or create a user profile by wallet address.
        
        Args:
            wallet_address: Ethereum wallet address
            user_data: Additional user data for new profiles
            
        Returns:
            UserProfile: Existing or newly created profile
        """
        if not self._initialized:
            await self.initialize()
            
        normalized = wallet_address.lower()
            
        # Try to get existing user
        profile = await self.get_user_by_wallet_address(normalized)
        if profile:
            return profile
            
        # Create new user
        profile = await self.database.user_profiles.get_or_create_by_wallet(normalized, user_data)
        
        # Add to memory cache
        self._profiles[profile.user_id] = profile
        
        if hasattr(profile, 'role') and profile.role:
            self.user_roles[profile.user_id] = profile.role
        
        if self.cache:
            await self.cache.set(f"user_profile:{profile.user_id}", profile)
            await self.cache.set(f"wallet_user:{normalized}", profile)
            
        return profile

    async def save_user_profile(self, profile: UserProfile) -> None:
        """Save a user profile."""
        if not self._initialized:
            await self.initialize()

        await self.database.user_profiles.upsert_user_profile(profile)
        
        self._profiles[profile.user_id] = profile
        
        if hasattr(profile, 'role') and profile.role:
            self.user_roles[profile.user_id] = profile.role
        
        if self.cache:
            await self.cache.set(f"user_profile:{profile.user_id}", profile)
            
            if profile.email:
                await self.cache.set(f"email_user:{profile.email}", profile)
            if profile.wallet_address:
                await self.cache.set(f"wallet_user:{profile.wallet_address}", profile)
                await self.cache.set(f"wallet_exists:{profile.wallet_address}", True)
            
        if hasattr(profile, 'role') and profile.role in ['admin', 'user']:
            self._admin_wallets_cache = None
            if self.cache:
                await self.cache.delete('admin_wallets')

    async def get_all_profiles(self) -> List[UserProfile]:
        """Get all user profiles."""
        if not self._initialized:
            await self.initialize()

        return await self.database.user_profiles.get_all()

    async def set_role(self, user_id: str, role: str) -> bool:
        """Set a user's role.

        Args:
            user_id: User ID to set role for
            role: Role to set ('user', 'admin')

        Returns:
            bool: True if role was set successfully
        """
        if not self._initialized:
            await self.initialize()

        # Validate role using core constants as single source of truth
        from core.constants import VALID_ROLES
        if role not in VALID_ROLES:
            self.logger.error(f"Invalid role: {role}. Must be one of: {VALID_ROLES}")
            return False
            
        try:
            profile = await self.get_user_profile(user_id)
                
            if not profile:
                self.logger.error(f"User {user_id} not found")
                return False
            
            old_role = profile.role
            profile.role = role
            profile.updated_at = datetime.utcnow()
            
            await self.save_user_profile(profile)
            self.user_roles[profile.user_id] = role
            
            if old_role != role:
                self._admin_wallets_cache = None
                if self.cache:
                    await self.cache.delete('admin_wallets')
                    
            self.logger.info(f"Set role '{role}' for user {user_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to set role for user {user_id}: {e}")
            return False
            
    async def get_role(self, user_id: str) -> str:
        """Get a user's role.
        
        Args:
            user_id: User ID to get role for
            
        Returns:
            str: User's role, defaults to 'user' if not found
        """
        if not self._initialized:
            await self.initialize()
            
        try:
            if user_id in self._profiles:
                return self._profiles[user_id].role or 'user'
                
            if user_id in self.user_roles:
                return self.user_roles[user_id]
                
            if self.cache:
                cached_profile = await self.cache.get(f"user_profile:{user_id}")
                if cached_profile and hasattr(cached_profile, 'role'):
                    return cached_profile.role or 'user'
            
            profile = await self.get_user_profile(user_id)
            if profile and profile.role:
                self.user_roles[user_id] = profile.role
                return profile.role
            
            return 'user'
            
        except Exception as e:
            self.logger.error(f"Failed to get role for user {user_id}: {e}")
            return 'user'
            
    async def get_admin_wallet_addresses(self) -> List[str]:
        """Get all wallet addresses with admin role.

        Returns:
            List[str]: List of wallet addresses with admin privileges
        """
        if not self._initialized:
            await self.initialize()
            
        try:
            if self._admin_wallets_cache is not None:
                return self._admin_wallets_cache
                
            if self.cache:
                cached = await self.cache.get('admin_wallets')
                if cached:
                    self._admin_wallets_cache = cached
                    return cached
            
            wallets = await self.database.user_profiles.get_admin_wallet_addresses()
            
            self._admin_wallets_cache = wallets
            if self.cache:
                await self.cache.set('admin_wallets', wallets)
                
            return wallets
            
        except Exception as e:
            self.logger.error(f"Failed to get admin wallet addresses: {e}")
            return []

    async def is_wallet_existing(self, wallet_address: str) -> bool:
        """Check if a wallet address is already registered.
        
        Args:
            wallet_address: Wallet address to check
            
        Returns:
            bool: True if wallet exists, False otherwise
        """
        if not self._initialized:
            await self.initialize()
            
        normalized = wallet_address.lower()
        
        if self.cache:
            cached_exists = await self.cache.get(f"wallet_exists:{normalized}")
            if cached_exists is not None:
                return cached_exists
                
        exists = await self.database.user_profiles.is_wallet_existing(normalized)
        
        if self.cache:
            await self.cache.set(f"wallet_exists:{normalized}", exists)
            
        return exists

    async def clear_cache(self, user_id: str) -> None:
        """Clear cached profile data for a user.
        
        Args:
            user_id: User ID to clear cache for
        """
        try:
            profile = await self.get_user_profile(user_id)
                
            if profile:
                if profile.user_id in self._profiles:
                    del self._profiles[profile.user_id]
                
                if profile.user_id in self.user_roles:
                    del self.user_roles[profile.user_id]
                
                if self.cache:
                    await self.cache.delete(f"user_profile:{profile.user_id}")
                    if profile.email:
                        await self.cache.delete(f"email_user:{profile.email}")
                    if profile.wallet_address:
                        await self.cache.delete(f"wallet_user:{profile.wallet_address}")
                        await self.cache.delete(f"wallet_exists:{profile.wallet_address}")
            else:
                if self.cache:
                    await self.cache.delete(f"user_profile:{user_id}")
                
            self.logger.info(f"Cleared cached profile data for user {user_id}")
            
        except Exception as e:
            self.logger.error(f"Failed to clear profile cache for user {user_id}: {e}")
            raise ComponentError(f"Failed to clear profile cache: {str(e)}")

    async def close(self) -> None:
        """Close the manager and cleanup resources."""
        if self.cache:
            await self.cache.close()
        self._initialized = False

    async def increment_sessions(self, user_id: str) -> bool:
        """Increment total sessions for a user.
        
        Args:
            user_id: User ID to increment sessions for
            
        Returns:
            bool: True if sessions were incremented successfully
        """
        if not self._initialized:
            await self.initialize()
            
        return await self.database.user_profiles.increment_user_sessions(user_id)

    async def set_tier(self, user_id: str, tier: str) -> bool:
        """Set a user's tier.

        Args:
            user_id: User ID to set tier for
            tier: Tier to set ('free', 'holder', 'x402', 'admin')

        Returns:
            bool: True if tier was set successfully
        """
        if not self._initialized:
            await self.initialize()
            
        return await self.database.user_profiles.set_tier(user_id, tier)

    async def update_token_count(self, user_id: str, token_count: int) -> bool:
        """Update user's DEN token count.
        
        Args:
            user_id: User ID to update
            token_count: New token count
            
        Returns:
            bool: True if updated successfully
        """
        if not self._initialized:
            await self.initialize()
            
        return await self.database.user_profiles.update_token_count(user_id, token_count)

    async def delete_user(self, user_id: str) -> bool:
        """Delete a user by ID.
        
        Args:
            user_id: User ID to delete
            
        Returns:
            bool: True if user was deleted
        """
        if not self._initialized:
            await self.initialize()
            
        try:
            profile = await self.get_user_profile(user_id)
            if not profile:
                return False
                
            self.logger.info(f"Deleting user {user_id}")
            
            # Delete related records
            try:
                await self.database.execute(
                    "DELETE FROM conversation_contexts WHERE user_id = ?",
                    (user_id,)
                )
            except Exception as e:
                self.logger.warning(f"Error deleting conversation contexts: {e}")
            
            try:
                await self.database.execute(
                    "DELETE FROM credit_transactions WHERE user_id = ?",
                    (user_id,)
                )
            except Exception as e:
                self.logger.warning(f"Error deleting credit transactions: {e}")
                
            try:
                await self.database.execute(
                    "DELETE FROM user_credits WHERE user_id = ?",
                    (user_id,)
                )
            except Exception as e:
                self.logger.warning(f"Error deleting user credits: {e}")
                
            # Delete user profile
            await self.database.execute(
                "DELETE FROM user_profiles WHERE user_id = ?",
                (user_id,)
            )
            
            # Clear caches
            if user_id in self._profiles:
                del self._profiles[user_id]
            if user_id in self.user_roles:
                del self.user_roles[user_id]
                
            if profile.role == 'admin':
                self._admin_wallets_cache = None
                
            await self.clear_cache(user_id)
            
            self.logger.info(f"Successfully deleted user {user_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to delete user {user_id}: {e}")
            raise ComponentError(f"Failed to delete user: {e}")
