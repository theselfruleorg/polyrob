"""OAuth manager (Item 4 — WS-G1, trimmed).

Holds registered ``OAuthProvider`` instances and a per-``(user_id, provider)`` token
store encrypted with the EXISTING Fernet helper (``tools/mcp/security.py::
MCPEncryption``). ``get_token`` returns a cached valid token, auto-refreshing on
expiry. Library-only — no tool migration; mirrors ``MemoryProviderRegistry``'s shape.
"""
from __future__ import annotations

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

    # -- main entrypoint ------------------------------------------------------

    async def get_token(self, user_id: str, provider: str) -> OAuthToken:
        """Return a valid token for ``(user_id, provider)``, refreshing if expired."""
        prov = self.get_provider(provider)
        token = self.load_token(user_id, provider)
        if token is None:
            raise OAuthError(f"no token stored for user='{user_id}' provider='{provider}'")
        if prov.valid_token(token):
            return token
        if not token.refresh_token:
            raise OAuthError(
                f"token expired and no refresh_token for user='{user_id}' provider='{provider}'"
            )
        refreshed = await prov.refresh(token)
        self.store_token(user_id, provider, refreshed)
        return refreshed
