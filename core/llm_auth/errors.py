"""Auth-shaped error taxonomy for LLM credentials (proposal 024, L1).

Extends the existing ``LLMAuthenticationError`` family (Tier-0 error taxonomy,
2026-07-22) with the fields an auth layer proves necessary in practice: a stable
``code``, the owning ``provider``, and ``relogin_required`` — so a refresh
failure ("connect the account again") is never conflated with a transient
throttle (HTTP 429), which must NOT tell the user to log in again.
"""
from __future__ import annotations

from typing import Optional

from core.exceptions import LLMAuthenticationError, LLMRateLimitError


class LLMAuthError(LLMAuthenticationError):
    """A credential-layer failure with structured routing fields."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "auth_error",
        provider: Optional[str] = None,
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider
        self.relogin_required = relogin_required


def is_rate_limited_auth_error(exc: BaseException) -> bool:
    """True when *exc* is a throttle, not a credential failure.

    A 429/rate-limit must never be misreported as "you need to log in again" —
    the lesson this predicate encodes.
    """
    if isinstance(exc, LLMRateLimitError):
        return True
    if isinstance(exc, LLMAuthError):
        return exc.code == "rate_limited"
    return False
