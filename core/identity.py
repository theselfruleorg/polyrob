"""Identity seam (R2). Local mode = one fixed identity; server resolves per-request.

The core never asks 'what mode am I in?' — it reads the injected identity. This
keeps user_id out of business-logic mode-branches.

Also houses pure, dependency-free tenant-id derivation helpers (relocated from
platform modules so core HTTP surfaces don't import the platform tier).
"""
from __future__ import annotations
import hashlib
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class IdentityProvider(Protocol):
    def resolve(self, request_context: Optional[dict] = None) -> str: ...


class LocalIdentity:
    """Single-user: always 'local'."""
    USER_ID = "local"

    def resolve(self, request_context: Optional[dict] = None) -> str:
        return self.USER_ID


class ConstantIdentity:
    """Any fixed id (tests / pinned tenants)."""
    def __init__(self, user_id: str):
        self._id = user_id

    def resolve(self, request_context: Optional[dict] = None) -> str:
        return self._id


# --- Anonymous/default tenant SSOT ------------------------------------------
# The canonical on-disk anon token. `agents.task.constants.DEFAULT_USER_ID` aliases
# this so the value is defined once (core must not import agents).
ANON_USER_ID = "_anonymous_"

# Non-empty strings that denote the shared anonymous/default bucket rather than a
# real, isolatable tenant. The synthetic server placeholders are here as a
# defense-in-depth backstop — the producing sites are being fixed to stop minting
# them (resolve to a real id, else ANON_USER_ID).
_ANON_SENTINELS = frozenset({
    ANON_USER_ID,
    "system",                  # cron/goal tool fallback
    "x402_user",               # synthetic x402 placeholder
    "authenticated_api_user",  # synthetic API-key placeholder
    "api_user",                # synthetic JWT placeholder
})


def normalize_user_id(user_id: object) -> str:
    """Pure normalization: None/blank -> '', otherwise the stripped string."""
    return ("" if user_id is None else str(user_id)).strip()


def is_anonymous(user_id: object) -> bool:
    """SSOT predicate: True when user_id is the anonymous/default bucket and must
    NOT be treated as an isolatable named tenant.

    True for None, '', the canonical token, and the synthetic sentinels.
    False for real named tenants like 'local', 'alice', 'u_<hex>'. In particular
    'local' (the CLI single-user tenant) is a real tenant, never anonymous.
    """
    norm = normalize_user_id(user_id)
    return norm == "" or norm in _ANON_SENTINELS


def resolve_identity() -> str:
    """Resolve the active local/CLI operator user id (a real, isolatable tenant).

    Used by the terminal-native surfaces (``polyrob goals``/``cron``, the REPL
    ``/goals`` handler, and CLI chat sessions — which route through this via
    ``ConstantIdentity``) to scope the data home + board to the operator. Prefers an
    explicitly-bound owner principal (``POLYROB_OWNER_USER_ID`` / ``BOT_OWNER_USER_ID``
    / ``SURFACE_SUPER_ADMIN_USER_IDS`` — see core.instance.resolve_owner_principal);
    otherwise the single-user local tenant ``'local'``. Never returns the anonymous
    bucket. Lazy import keeps core.identity free of a core.instance module cycle.
    """
    try:
        from core.instance import resolve_owner_principal
        # STRICT resolution (default_to_instance=False): None unless an owner is
        # EXPLICITLY bound. The default `default_to_instance=True` behavior falls
        # back to the instance id (DEFAULT_INSTANCE_ID = "rob"), which is never
        # anonymous — that would make the "local" fallback below unreachable and
        # contradict this function's own contract (owner-if-bound else "local").
        owner = resolve_owner_principal(default_to_instance=False)
        if owner and not is_anonymous(owner):
            return owner
    except Exception:
        pass
    return LocalIdentity.USER_ID


# --- Wallet-to-tenant derivation (x402 variant) --------------------------------
# Relocated from modules/x402/x402_integration.py so polyrob-core surfaces
# (api/task_http_api.py, api/a2a/streaming.py, api/dependencies.py) can derive a
# wallet-scoped tenant id without importing the platform x402 tier.
#
# NOTE: This is the `usr_` + 12-hex variant. Another wallet->id derivation exists
# with DIFFERENT output and is deliberately NOT touched:
#   modules/database/user_profiles.py   → usr_ + 16 hex  (different id space)
# Unifying them would re-tenant existing users — a data hazard. (A third variant,
# modules/memory/user_profile_manager.py, was dead code deleted 2026-07-11.)
def generate_user_id_from_wallet(wallet_address: str) -> str:
    """Generate a consistent user_id from a wallet address (usr_ + 12 hex chars).

    Args:
        wallet_address: Ethereum wallet address (0x...)

    Returns:
        User ID in format usr_<12 hex chars> derived from sha256 of lowercased address.
    """
    normalized = wallet_address.lower()
    hash_bytes = hashlib.sha256(normalized.encode()).hexdigest()
    return f"usr_{hash_bytes[:12]}"
