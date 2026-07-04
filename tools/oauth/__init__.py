"""Unified OAuth manager (Item 4 — minimal WS-G).

Library-only: an ``OAuthProvider`` seam + ``OAuthManager`` registry on the existing
Fernet token store, plus one ``GenericOAuth2Provider``. No existing tool is migrated
(deferred). Mint/store(encrypted)/refresh a token per user through the manager.
"""
from tools.oauth.provider import OAuthError, OAuthProvider, OAuthToken
from tools.oauth.manager import OAuthManager
from tools.oauth.providers.generic_oauth2 import GenericOAuth2Provider

__all__ = [
    "OAuthError",
    "OAuthProvider",
    "OAuthToken",
    "OAuthManager",
    "GenericOAuth2Provider",
]
