"""API key manager for programmatic access."""

import secrets
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

from core.exceptions import AuthError

logger = logging.getLogger(__name__)


class APIKeyManager:
    """Manage API keys for programmatic access."""

    def __init__(self, db, tier_manager):
        """
        Initialize API key manager.

        Args:
            db: Database manager instance
            tier_manager: TierManager instance
        """
        self.db = db
        self.tier_manager = tier_manager
        self.logger = logging.getLogger('auth.api_key_manager')

    async def generate_api_key(self, user_id: str, name: str = "Default",
                               expires_days: Optional[int] = None) -> dict:
        """
        Generate new API key for user.

        BETA: User must have DEN token to generate API keys.

        Args:
            user_id: User ID
            name: Name for the API key
            expires_days: Number of days until expiration (None = never expires)

        Returns:
            Dict with api_key and metadata

        Raises:
            ValueError: If user doesn't have DEN token
        """

        # Verify user has tier (= has DEN token)
        try:
            tier = await self.tier_manager.get_user_tier(user_id)
        except AuthError:
            raise ValueError("API keys require DEN token ownership")

        # Generate secure key
        key = f"rob_{secrets.token_urlsafe(32)}"

        # Hash for storage
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        key_prefix = key[:12]  # rob_abc123...

        # Calculate expiry
        expires_at = None
        if expires_days:
            expires_at = datetime.now() + timedelta(days=expires_days)

        # Store in database
        await self.db.execute("""
            INSERT INTO api_keys (
                user_id, key_hash, key_prefix, name,
                created_at, expires_at, is_active
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, 1)
        """, (user_id, key_hash, key_prefix, name, expires_at))

        self.logger.info(f"Generated new API key for user {user_id}: {key_prefix}...")

        # Return key ONCE (never stored in plain text)
        return {
            "api_key": key,
            "name": name,
            "prefix": key_prefix,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "created_at": datetime.now().isoformat(),
            "warning": "Store this key securely - it won't be shown again!"
        }

    async def validate_api_key(self, api_key: str) -> Optional[str]:
        """
        Validate API key and return user_id.

        Args:
            api_key: API key to validate

        Returns:
            user_id if valid, None otherwise
        """

        # Hash the provided key
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        # Look up in database
        result = await self.db.fetch_one("""
            SELECT user_id, expires_at, is_active
            FROM api_keys
            WHERE key_hash = ? AND is_active = 1
        """, (key_hash,))

        if not result:
            return None

        # Check if expired
        if result['expires_at']:
            expires_at = datetime.fromisoformat(result['expires_at'])
            if expires_at < datetime.now():
                self.logger.warning(f"API key expired: {key_hash[:8]}...")
                return None

        # Update last_used
        await self.db.execute("""
            UPDATE api_keys
            SET last_used = CURRENT_TIMESTAMP
            WHERE key_hash = ?
        """, (key_hash,))

        return result['user_id']

    async def list_user_keys(self, user_id: str) -> list:
        """List user's API keys."""

        results = await self.db.fetch_all("""
            SELECT
                key_prefix,
                name,
                created_at,
                last_used,
                expires_at,
                is_active
            FROM api_keys
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,))

        return [dict(row) for row in results]

    async def revoke_key(self, user_id: str, key_prefix: str) -> bool:
        """Revoke an API key."""

        result = await self.db.execute("""
            UPDATE api_keys
            SET is_active = 0, revoked_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND key_prefix = ?
        """, (user_id, key_prefix))

        if result.rowcount > 0:
            self.logger.info(f"Revoked API key {key_prefix} for user {user_id}")
            return True

        return False
