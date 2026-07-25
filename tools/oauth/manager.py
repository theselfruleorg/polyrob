"""OAuth manager (Item 4 — WS-G1, trimmed).

Holds registered ``OAuthProvider`` instances and a per-``(user_id, provider)`` token
store encrypted with the EXISTING Fernet helper (``tools/mcp/security.py::
MCPEncryption``). ``get_token`` returns a cached valid token, auto-refreshing on
expiry. Library-only — no tool migration; mirrors ``MemoryProviderRegistry``'s shape.

**Refresh is serialized per (user_id, provider)** (T2.4 review fast-follow):
without a lock, two concurrent callers observing the same expired/invalid token
(e.g. two in-flight MCP requests both getting a 401 around the same time —
``tools/mcp/oauth_bridge.py``'s 401-retry-once callback calls
:meth:`force_refresh`) could both call the provider's refresh endpoint. Most
OAuth2 providers rotate a single-use ``refresh_token`` on every refresh, so the
loser of that race gets its refresh REJECTED (its refresh_token was already
consumed by the winner) — or worse, if it "wins" a later write, it clobbers a
freshly-refreshed token in the store with a stale one derived from the
now-invalid refresh_token. Both :meth:`get_token`'s natural expiry-refresh path
and :meth:`force_refresh` funnel through the SAME per-key
:class:`asyncio.Lock` and the SAME identity-guarded refresh helper
(:meth:`_refresh_from`), so concurrent callers collapse into exactly ONE
provider refresh call: whoever acquires the lock second sees the token the
first caller already stored and returns it as-is, instead of refreshing again.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, MutableMapping, Optional, Tuple

from tools.oauth.provider import OAuthError, OAuthProvider, OAuthToken

Key = Tuple[str, str]


class OAuthManager:
    """Provider registry + encrypted token store keyed by ``(user_id, provider)``."""

    def __init__(self, encryption: Any = None, store: Optional[MutableMapping[Key, bytes]] = None) -> None:
        self._providers: Dict[str, OAuthProvider] = {}
        # Encrypted-at-rest store: (user_id, provider) -> Fernet ciphertext bytes.
        self._store: MutableMapping[Key, bytes] = store if store is not None else {}
        self._encryption = encryption or self._default_encryption()
        # Per-(user_id, provider) refresh lock — see class docstring.
        self._locks: Dict[Key, asyncio.Lock] = {}

    @staticmethod
    def _default_encryption():
        from tools.mcp.security import MCPEncryption
        return MCPEncryption()

    # -- provider registry ----------------------------------------------------

    def register(self, provider: OAuthProvider) -> OAuthProvider:
        self._providers[provider.name] = provider
        return provider

    def get_provider(self, name: str) -> OAuthProvider:
        prov = self._providers.get(name)
        if prov is None:
            raise OAuthError(
                f"unknown oauth provider '{name}' (known: {sorted(self._providers)})"
            )
        return prov

    # -- encrypted token store ------------------------------------------------

    def store_token(self, user_id: str, provider: str, token: OAuthToken) -> None:
        self._store[(user_id, provider)] = self._encryption.encrypt_dict(token.to_dict())

    def load_token(self, user_id: str, provider: str) -> Optional[OAuthToken]:
        blob = self._store.get((user_id, provider))
        if not blob:
            return None
        return OAuthToken.from_dict(self._encryption.decrypt_dict(blob))

    def has_token(self, user_id: str, provider: str) -> bool:
        return (user_id, provider) in self._store

    # -- refresh serialization --------------------------------------------------

    def _lock_for(self, user_id: str, provider: str) -> asyncio.Lock:
        key = (user_id, provider)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _refresh_from(
        self, user_id: str, provider: str, prov: OAuthProvider, observed: OAuthToken
    ) -> OAuthToken:
        """Refresh ``observed`` under the per-``(user_id, provider)`` lock.

        Identity-guarded: if the token currently in the store no longer
        matches ``observed.access_token`` by the time the lock is acquired,
        another coroutine already refreshed it (or something else replaced
        it) — that current token is returned as-is rather than refreshing a
        second time. This is what collapses N concurrent callers reacting to
        the SAME stale token into exactly one provider refresh call.
        """
        async with self._lock_for(user_id, provider):
            current = self.load_token(user_id, provider)
            if current is not None and current.access_token != observed.access_token:
                return current
            if current is None or not current.refresh_token:
                raise OAuthError(
                    f"token invalid/expired and no refresh_token for user='{user_id}' provider='{provider}'"
                )
            refreshed = await prov.refresh(current)
            self.store_token(user_id, provider, refreshed)
            return refreshed

    # -- main entrypoint ------------------------------------------------------

    async def get_token(self, user_id: str, provider: str) -> OAuthToken:
        """Return a valid token for ``(user_id, provider)``, refreshing if expired."""
        prov = self.get_provider(provider)
        token = self.load_token(user_id, provider)
        if token is None:
            raise OAuthError(f"no token stored for user='{user_id}' provider='{provider}'")
        if prov.valid_token(token):
            return token
        return await self._refresh_from(user_id, provider, prov, token)

    async def force_refresh(self, user_id: str, provider: str) -> OAuthToken:
        """Unconditionally refresh, even if the stored token still looks
        locally valid — used when the SERVER says the token is bad (e.g. a
        401) despite our clock-based ``valid_token`` bookkeeping. Serialized
        through the same per-key lock and identity guard as :meth:`get_token`
        (see class docstring): concurrent callers reacting to the same 401
        collapse into exactly one provider refresh call.
        """
        prov = self.get_provider(provider)
        token = self.load_token(user_id, provider)
        if token is None:
            raise OAuthError(f"no token stored for user='{user_id}' provider='{provider}'")
        return await self._refresh_from(user_id, provider, prov, token)
