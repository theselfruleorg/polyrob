"""OAuth provider seam (Item 4 — WS-G1, trimmed).

An ``OAuthProvider`` knows how to build an authorize URL, exchange an auth code for
tokens, and refresh them. ``OAuthToken`` is the normalised token shape stored
(encrypted) by the ``OAuthManager``.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


class OAuthError(RuntimeError):
    """Raised for unknown-provider / missing-token / refresh failures."""


@dataclass
class OAuthToken:
    """A normalised OAuth2 token set."""
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None  # epoch seconds; None = non-expiring
    token_type: str = "Bearer"
    scope: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OAuthToken":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        extra = {k: d.pop(k) for k in list(d) if k not in known}
        token = cls(**{k: v for k, v in d.items() if k in known})
        if extra:
            token.extra.update(extra)
        return token


class OAuthProvider(ABC):
    """Auth-code + refresh-token OAuth2 provider."""

    name: str = "oauth"

    #: refresh this many seconds before actual expiry
    leeway_sec: float = 60.0

    @abstractmethod
    def authorize_url(self, *, state: Optional[str] = None, redirect_uri: Optional[str] = None) -> str:
        """Build the user-facing authorization URL."""

    @abstractmethod
    async def exchange_code(self, code: str, *, redirect_uri: Optional[str] = None) -> OAuthToken:
        """Exchange an authorization code for a token set."""

    @abstractmethod
    async def refresh(self, token: OAuthToken) -> OAuthToken:
        """Exchange a refresh token for a fresh token set."""

    def valid_token(self, token: Optional[OAuthToken]) -> bool:
        """True if ``token`` is present and not within ``leeway_sec`` of expiry."""
        if not token or not token.access_token:
            return False
        if token.expires_at is None:
            return True
        return time.time() < (token.expires_at - self.leeway_sec)
