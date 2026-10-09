"""API key manager for programmatic access."""

import secrets
import hashlib
import logging
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.exceptions import AuthError
from core.security.api_keys import DEFAULT_SCOPES, DEFAULT_EXPIRY_DAYS, MAX_EXPIRY_DAYS
from core.security.api_keys import scopes as validate_scopes, expiry_timestamp

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
                               expires_days: int = DEFAULT_EXPIRY_DAYS,
                               scopes=DEFAULT_SCOPES) -> dict:
        """
        Generate new API key for user.

        BETA: User must have DEN token to generate API keys.

        Args:
            user_id: User ID
            name: Name for the API key
            expires_days: Number of days until expiration (1–365; default 90)

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
        if tier not in ('holder', 'admin', 'free_access', 'x402'):
            raise ValueError("API keys require an eligible account tier")
        if type(expires_days) is not int or not 1 <= expires_days <= MAX_EXPIRY_DAYS:
            raise ValueError('API key expiry must be between 1 and 365 days')
        granted_scopes = validate_scopes(scopes)

        # Generate secure key
        key = f"rob_{secrets.token_urlsafe(32)}"

        # Hash for storage
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        key_prefix = key[:12]  # rob_abc123...

        # Calculate expiry
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=expires_days)

        # Store in database
        await self.db.execute("""
            INSERT INTO api_keys (
                user_id, key_hash, key_prefix, name,
                created_at, expires_at, is_active, scopes
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, 1, ?)
        """, (user_id, key_hash, key_prefix, name, expires_at.isoformat(), json.dumps(granted_scopes)))

        self.logger.info(f"Generated new API key for user {user_id}: {key_prefix}...")

        # Return key ONCE (never stored in plain text)
        return {
            "api_key": key,
            "name": name,
            "prefix": key_prefix,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "created_at": now.isoformat(),
            "scopes": granted_scopes,
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
            SELECT user_id, expires_at, is_active, scopes
            FROM api_keys
            WHERE key_hash = ? AND is_active = 1
        """, (key_hash,))

        if not result:
            return None

        # Check if expired
        try:
            validate_scopes(result['scopes'])
            if expiry_timestamp(result['expires_at']) <= datetime.now(timezone.utc).timestamp():
                return None
        except (ValueError, TypeError):
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
                is_active,
                scopes
            FROM api_keys
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,))

        keys = []
        for row in results:
            info = dict(row)
            info['prefix'] = info.pop('key_prefix')
            try:
                info['scopes'] = validate_scopes(info['scopes'])
            except (ValueError, TypeError):
                info['scopes'] = []  # Legacy unscoped keys must be replaced.
            keys.append(info)
        return keys

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
