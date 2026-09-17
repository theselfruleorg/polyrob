"""Per-user credential cache for a tool that loads credentials from its db.

``HyperliquidTool`` and ``PolymarketTool`` each carried the same
``_get_user_credentials``: no user -> None; a cached hit -> the hit; else
``self.db.get_credentials(user_id)``, cached when found. One mixin.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class UserCredentialCacheMixin:
    _user_id: Optional[str] = None
    _credentials_cache: Dict[str, Any]
    db: Any = None

    async def _get_user_credentials(self):
        """Credentials for the current user, from the cache else the db."""
        if not self._user_id:
            return None
        cached = self._credentials_cache.get(self._user_id)
        if cached is not None:
            return cached
        if self.db:
            credentials = await self.db.get_credentials(self._user_id)
            if credentials:
                self._credentials_cache[self._user_id] = credentials
            return credentials
        return None
