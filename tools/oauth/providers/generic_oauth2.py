"""Generic OAuth2 auth-code + refresh-token provider (Item 4).

Driven by a plain config dict (client_id/secret/auth_url/token_url/scopes/
redirect_uri). The HTTP token POST is injectable (``http_post``) so the provider is
unit-testable WITHOUT hitting any live endpoint; the default uses ``httpx``.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlencode

from tools.oauth.provider import OAuthError, OAuthProvider, OAuthToken

# (url, form_data) -> token-endpoint JSON dict
HttpPost = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class GenericOAuth2Provider(OAuthProvider):
    def __init__(self, name: str, config: Dict[str, Any], *, http_post: Optional[HttpPost] = None) -> None:
        self.name = name
        self.client_id = config["client_id"]
        self.client_secret = config.get("client_secret", "")
        self.auth_url = config["auth_url"]
        self.token_url = config["token_url"]
        self.scopes = config.get("scopes") or []
        self.redirect_uri = config.get("redirect_uri")
        self._http_post = http_post

    def authorize_url(self, *, state: Optional[str] = None, redirect_uri: Optional[str] = None) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri or self.redirect_uri or "",
        }
        if self.scopes:
            params["scope"] = " ".join(self.scopes)
        if state:
            params["state"] = state
        sep = "&" if "?" in self.auth_url else "?"
        return f"{self.auth_url}{sep}{urlencode(params)}"

    async def exchange_code(self, code: str, *, redirect_uri: Optional[str] = None) -> OAuthToken:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri or self.redirect_uri or "",
        }
        return self._token_from_response(await self._post_token(data))

    async def refresh(self, token: OAuthToken) -> OAuthToken:
        if not token.refresh_token:
            raise OAuthError(f"no refresh_token to refresh provider '{self.name}'")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        new = self._token_from_response(await self._post_token(data))
        # Many providers don't rotate the refresh token — keep the old one if absent.
        if not new.refresh_token:
            new.refresh_token = token.refresh_token
        return new

    # -- internals ------------------------------------------------------------

    async def _post_token(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if self._http_post is not None:
            return await self._http_post(self.token_url, data)
        import httpx  # lazy: only needed for the live path

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.token_url, data=data)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _token_from_response(resp: Dict[str, Any]) -> OAuthToken:
        if not resp or "access_token" not in resp:
            raise OAuthError(f"token endpoint returned no access_token: {resp}")
        expires_at = None
        if "expires_in" in resp:
            try:
                expires_at = time.time() + float(resp["expires_in"])
            except (TypeError, ValueError):
                expires_at = None
        return OAuthToken(
            access_token=resp["access_token"],
            refresh_token=resp.get("refresh_token"),
            expires_at=expires_at,
            token_type=resp.get("token_type", "Bearer"),
            scope=resp.get("scope"),
        )
