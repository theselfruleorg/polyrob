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
import secrets
import time
from typing import Any, Dict, MutableMapping, Optional, Tuple

from core.state.machine import StateMachine
from tools.oauth.provider import OAuthError, OAuthProvider, OAuthToken

Key = Tuple[str, str]

#: 098 TS-13. A refresh the provider REFUSED (invalid_grant/invalid_client) is a
#: stored fact, not an exception to repeat: NEEDS_REAUTH accepts only a new
#: authorization. A transient (network) failure keeps the token as it was.
TOKEN = StateMachine(
    "oauth.token",
    states={"absent", "valid", "refreshing", "needs_reauth"},
    transitions={"absent": {"valid"}, "valid": {"refreshing"},
                 "refreshing": {"valid", "needs_reauth"}, "needs_reauth": {"valid"}},
    initial={"absent"},
)
NEEDS_REAUTH = "needs_reauth"
_REFUSALS = ("invalid_grant", "invalid_client", "unauthorized_client")


def _refused(e: BaseException) -> bool:
    """True when the provider refused the refresh token itself (re-authorize),
    False for a transient failure (network, 5xx)."""
    body = str(e) + " " + str(getattr(getattr(e, "response", None), "text", "") or "")
    return any(code in body for code in _REFUSALS)


class OAuthManager:
    """Provider registry + encrypted token store keyed by ``(user_id, provider)``."""

    def __init__(self, encryption: Any = None, store: Optional[MutableMapping[Key, bytes]] = None) -> None:
        self._providers: Dict[str, OAuthProvider] = {}
        # Encrypted-at-rest store: (user_id, provider) -> Fernet ciphertext bytes.
        self._store: MutableMapping[Key, bytes] = store if store is not None else {}
        self._encryption = encryption or self._default_encryption()
        # Per-(user_id, provider) refresh lock — see class docstring.
        self._locks: Dict[Key, asyncio.Lock] = {}
        self._authorizations: dict[str, tuple] = {}

    def begin_authorization(self, user_id: str, provider: str, *, redirect_uri: Optional[str] = None) -> str:
        """Create a bounded, ten-minute PKCE transaction bound to this principal."""
        now = time.monotonic()
        self._authorizations = {state: row for state, row in self._authorizations.items()
                                if row[0] > now}
        if len(self._authorizations) >= 128:
            raise OAuthError('Too many pending OAuth authorizations')
        if not user_id:
            raise OAuthError('OAuth authorization requires a user identity')
        prov = self.get_provider(provider)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        redirect = redirect_uri or getattr(prov, 'redirect_uri', None)
        if not redirect:
            raise OAuthError('OAuth authorization requires a redirect URI')
        url = prov.authorize_url(state=state, code_verifier=verifier, redirect_uri=redirect)
        # Keep this exact provider object: re-registering the name must not send
        # an in-flight code/verifier to a different endpoint.
        self._authorizations[state] = (now + 600, user_id, provider, prov, verifier, redirect)
        return url

    async def finish_authorization(self, user_id: str, provider: str, *, state: str, code: str) -> OAuthToken:
        """Consume state before the HTTP exchange; expired, wrong-user and replayed callbacks refuse."""
        if not isinstance(state, str):
            raise OAuthError('Invalid OAuth state')
        row = self._authorizations.get(state)
        if row is None or row[0] <= time.monotonic() or row[1:3] != (user_id, provider):
            raise OAuthError('Invalid, expired or mismatched OAuth state')
        if not isinstance(code, str) or not code or len(code) > 8192:
            raise OAuthError('Invalid OAuth authorization code')
        del self._authorizations[state]  # no await before consumption
        _, _, _, prov, verifier, redirect = row
        token = await prov.exchange_code(code, code_verifier=verifier, redirect_uri=redirect)
        self.store_token(user_id, provider, token)  # a new grant clears needs_reauth
        try:
            from core.credential_verdicts import clear_rejection
            clear_rejection("oauth", f"{provider}:{user_id}")
        except Exception:
            pass
        return token

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

    def state(self, user_id: str, provider: str) -> str:
        """``absent`` | ``valid`` | ``needs_reauth`` (098 TS-13)."""
        token = self.load_token(user_id, provider)
        if token is None:
            return "absent"
        return NEEDS_REAUTH if token.extra.get("oauth_state") == NEEDS_REAUTH else "valid"

    def _refuse_if_needs_reauth(self, user_id: str, provider: str, token: OAuthToken) -> None:
        if token.extra.get("oauth_state") == NEEDS_REAUTH:
            raise OAuthError(f"re-authorize {provider}: the provider refused the refresh "
                             f"token ({token.extra.get('oauth_error') or 'invalid_grant'})")

    def _mark_needs_reauth(self, user_id: str, provider: str, token: OAuthToken,
                           error: BaseException) -> None:
        TOKEN.check("refreshing", NEEDS_REAUTH, ctx={"provider": provider})
        code = next((c for c in _REFUSALS if c in str(error)), "invalid_grant")
        token.extra.update({"oauth_state": NEEDS_REAUTH, "oauth_error": code,
                            "oauth_since": time.time()})
        self.store_token(user_id, provider, token)
        try:  # the owner sees ONE actionable item, not a stream of failed connects
            from core.credential_verdicts import record_rejection
            record_rejection("oauth", f"{provider}:{user_id}", code=NEEDS_REAUTH,
                             remedy=f"re-authorize {provider}")
        except Exception:
            pass

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
            if current is not None:
                self._refuse_if_needs_reauth(user_id, provider, current)
            if current is not None and current.access_token != observed.access_token:
                return current
            if current is None or not current.refresh_token:
                raise OAuthError(
                    f"token invalid/expired and no refresh_token for user='{user_id}' provider='{provider}'"
                )
            try:
                refreshed = await prov.refresh(current)
            except Exception as e:
                if _refused(e):
                    # 098 review: a peer PROCESS may have rotated the refresh
                    # token under us — its fresh token in the store is the
                    # answer, never a needs_reauth latch over it.
                    latest = self.load_token(user_id, provider)
                    if latest is not None and latest.access_token != current.access_token:
                        self._refuse_if_needs_reauth(user_id, provider, latest)
                        return latest
                    self._mark_needs_reauth(user_id, provider, current, e)
                    raise OAuthError(f"re-authorize {provider}: {e}") from e
                raise
            self.store_token(user_id, provider, refreshed)
            return refreshed

    # -- main entrypoint ------------------------------------------------------

    async def get_token(self, user_id: str, provider: str) -> OAuthToken:
        """Return a valid token for ``(user_id, provider)``, refreshing if expired."""
        prov = self.get_provider(provider)
        token = self.load_token(user_id, provider)
        if token is None:
            raise OAuthError(f"no token stored for user='{user_id}' provider='{provider}'")
        self._refuse_if_needs_reauth(user_id, provider, token)
        if prov.valid_token(token):
            return token
        return await self._refresh_from(user_id, provider, prov, token)

    async def force_refresh(self, user_id: str, provider: str,
                            observed_access_token: Optional[str] = None) -> OAuthToken:
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
        if observed_access_token:
            # 098 TS-13: the token the CALLER sent (the 401'd one) is the guard,
            # so a caller whose token a peer already replaced gets the new one.
            token = OAuthToken(access_token=observed_access_token)
        return await self._refresh_from(user_id, provider, prov, token)
