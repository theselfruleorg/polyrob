"""Fresh credential reads with signing-client invalidation when policy changes."""
from __future__ import annotations

from typing import Any, Dict, Optional


class UserCredentialCacheMixin:
    _user_id: Optional[str] = None
    _credentials_cache: Dict[str, Any]
    db: Any = None

    async def _get_user_credentials(self):
        """Re-read policy and revocation state before using a cached signing client."""
        if not self._user_id:
            return None
        if self.db:
            credentials = await self.db.get_credentials(self._user_id)
            if credentials != self._credentials_cache.get(self._user_id):
                for name in ("_exchange_clients", "_clob_clients"):
                    getattr(self, name, {}).clear()
            if credentials:
                self._credentials_cache[self._user_id] = credentials
            else:
                self._credentials_cache.pop(self._user_id, None)
            return credentials
        return None
