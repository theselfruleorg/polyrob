"""LLM credential layer (proposal 024, L1) — store, resolution oracle, health.

Owner-only surface: no agent action touches this package (permanent non-goal,
pinned by tests/unit/core/llm_auth/test_agent_unreachable.py), and the store
file itself is name-denied to agent file tools via core/security/secret_guard.
"""
from core.llm_auth.errors import LLMAuthError, is_rate_limited_auth_error
from core.llm_auth.health import (
    HEALTH_EXHAUSTED,
    HEALTH_OK,
    HEALTH_RATE_LIMITED,
    effective_state,
    is_usable,
    stamp_from_error_verdict,
)
from core.llm_auth.resolve import (
    Credential,
    auth_store_enabled,
    credential_borrow_enabled,
    resolve_credential,
)
from core.llm_auth.store import AuthStore, AuthStoreError, auth_store_path, get_auth_store

__all__ = [
    "AuthStore",
    "AuthStoreError",
    "Credential",
    "HEALTH_EXHAUSTED",
    "HEALTH_OK",
    "HEALTH_RATE_LIMITED",
    "LLMAuthError",
    "auth_store_enabled",
    "auth_store_path",
    "credential_borrow_enabled",
    "effective_state",
    "get_auth_store",
    "is_rate_limited_auth_error",
    "is_usable",
    "resolve_credential",
    "stamp_from_error_verdict",
]
