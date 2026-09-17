"""The single credential-resolution oracle (proposal 024, L1).

``resolve_credential(provider, spec)`` — precedence:

1. explicit env key (``spec.env_key`` present and value passes the injected
   validator);
2. ``auth.json`` provider entry (OAuth token; L2 owns refresh — an entry past
   its expiry resolves with ``relogin_required`` health rather than refreshing
   here);
3. ``auth.json`` borrowed entry (only with ``LLM_CREDENTIAL_BORROW`` on);
4. ``spec.auth_type == "none"`` → a sentinel credential (Ollama loopback);
5. ``None``.

Layering: this is core-tier and imports NO ``modules.*`` code (5-tier ratchet).
The ProviderSpec is duck-typed — only ``env_key``/``auth_type`` attributes are
read — and the key-shape validator is injected by the modules-side caller
(``modules.llm.profiles.looks_like_real_key``) so the shared definition stays
single-sourced without an upward import.

Tenancy (024 §7.3, fail closed): store-backed rungs 2-3 serve credentials only
when ``LLM_AUTH_STORE_ENABLED`` is on AND the deployment is the single-owner
local profile (``POLYROB_LOCAL``). An accidental multi-tenant server deploy
degrades to env-key behavior — it can never share the owner's seat.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.env import bool_env, parse_bool
from core.llm_auth.health import HEALTH_OK, effective_state

KIND_API_KEY = "api_key"
KIND_BEARER = "bearer"
KIND_NONE = "none"

SOURCE_ENV = "env"
SOURCE_OAUTH = "oauth"
SOURCE_BORROWED = "borrowed"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class Credential:
    kind: str
    value: Optional[str] = field(repr=False, default=None)
    provider: str = ""
    source: str = SOURCE_ENV
    health: str = HEALTH_OK
    expires_at: Optional[float] = None
    relogin_required: bool = False

    def __repr__(self) -> str:  # never leak the value (mirrors WalletConfig)
        return (
            f"Credential(kind={self.kind!r}, provider={self.provider!r}, "
            f"source={self.source!r}, health={self.health!r}, value=<redacted>)"
        )

    def redacted(self, keep: int = 4) -> str:
        """A display-safe fingerprint of the value — THE only printable form.

        `polyrob auth status` and friends need to show enough for an owner to
        tell two credentials apart ("is that the key I just pasted?") without
        printing one. Short values redact whole: for anything under ~3x `keep`,
        a prefix+suffix reveals most of it.

        Callers must never format ``self.value`` directly. This method exists so
        that rule has somewhere to go.
        """
        from core.security.redaction import fingerprint
        return fingerprint(self.value, keep=keep)


def auth_store_enabled(env=None) -> bool:
    source = env if env is not None else _os_environ()
    raw = source.get("LLM_AUTH_STORE_ENABLED")
    if raw is not None and str(raw).strip() != "":
        return parse_bool(raw, False)
    # 027 WP4: default ON under the single-user local profile — an
    # `auth add <oauth-seat>` connect must be readable without a second,
    # undocumented flag. Multi-tenant servers (no POLYROB_LOCAL) stay OFF,
    # and _store_rungs_allowed's local_mode gate still backstops them.
    try:
        from core.config_policy.policy import local_mode_enabled

        return local_mode_enabled()
    except Exception:
        return False


def _os_environ():
    import os

    return os.environ


def credential_borrow_enabled(env=None) -> bool:
    if env is not None:
        return parse_bool(env.get("LLM_CREDENTIAL_BORROW"), False)
    return bool_env("LLM_CREDENTIAL_BORROW", False)


def _store_rungs_allowed(env=None) -> bool:
    """Fail-closed tenancy gate for the store-backed rungs (024 §7.3)."""
    if not auth_store_enabled(env):
        return False
    try:
        from core.config_policy.policy import local_mode_enabled
        if not local_mode_enabled():
            return False  # multi-tenant server: env-key behavior only (024b marker)
    except Exception:
        return False
    return True


def _tenant_allowed(user_id: Optional[str]) -> bool:
    """024 §7.3: a KNOWN non-owner tenant never gets a store credential.

    ``None`` = owner-context caller (CLI/local process). Fail-closed: if the
    owner check itself errors, refuse.
    """
    if user_id is None:
        return True
    try:
        from core.instance import is_owner
        return is_owner(user_id)
    except Exception:
        return False


def _env_key_chain(spec: Any) -> tuple:
    """Every env var name *spec* accepts, primary first (duck-typed).

    Prefers an ``env_key_chain()`` method when the object has one, and falls
    back to the single ``env_key`` attribute so any spec-shaped object works.
    """
    if spec is None:
        return ()
    chain = getattr(spec, "env_key_chain", None)
    if callable(chain):
        try:
            return tuple(n for n in chain() if n)
        except Exception:
            pass
    name = getattr(spec, "env_key", None)
    return (name,) if name else ()


def resolve_credential(
    provider: str,
    spec: Any = None,
    *,
    env=None,
    key_validator: Optional[Callable[[Any], bool]] = None,
    store=None,
    now: Optional[float] = None,
    user_id: Optional[str] = None,
) -> Optional[Credential]:
    """Resolve *provider*'s effective credential, or None.

    ``spec`` is duck-typed (``env_key`` / ``auth_type`` attributes; enum values
    compare by ``.value`` or plain string). ``key_validator`` defaults to
    plain truthiness; modules-side callers inject ``looks_like_real_key``.

    ``user_id`` (024 §7.3): when the caller knows the requesting TENANT, pass
    it — the store rungs are refused for any non-owner tenant, so a
    multi-surface deployment can never serve the owner's subscription seat to a
    correspondent/third-party principal. ``None`` means "owner-context caller"
    (the CLI/local process paths).
    """
    env = os.environ if env is None else env
    validate = key_validator or (lambda v: bool(v))
    now = time.time() if now is None else now

    auth_type = getattr(spec, "auth_type", None)
    auth_type_value = getattr(auth_type, "value", auth_type)

    # 1. explicit env key. A provider may answer to SEVERAL var names (z.ai:
    #    GLM_API_KEY / ZAI_API_KEY / Z_AI_API_KEY; Copilot: COPILOT_GITHUB_TOKEN
    #    / GH_TOKEN / GITHUB_TOKEN) — try them in the spec's declared order.
    #    Duck-typed like the rest of `spec`: an object exposing only `env_key`
    #    still works, so this stays usable with a bare ProviderProfile.
    for env_key in _env_key_chain(spec):
        value = env.get(env_key)
        if value and validate(value):
            return Credential(
                kind=KIND_API_KEY, value=str(value), provider=provider, source=SOURCE_ENV
            )

    # 2-3. store-backed rungs (gated + fail-closed; non-owner tenants refused)
    if _store_rungs_allowed(env) and _tenant_allowed(user_id):
        try:
            if store is None:
                from core.llm_auth.store import get_auth_store
                store = get_auth_store()
            entry = store.get_provider(provider)
        except Exception:
            entry = None
        if entry and entry.get("access_token"):
            expires_at = entry.get("expires_at")
            expired = expires_at is not None and now >= float(expires_at)
            return Credential(
                kind=KIND_BEARER,
                value=str(entry["access_token"]),
                provider=provider,
                source=SOURCE_OAUTH,
                health=effective_state(entry.get("health"), now),
                expires_at=float(expires_at) if expires_at is not None else None,
                # L2 owns refresh; until it lands ANY expired token is flagged
                # relogin_required — a stale bearer must never be served as
                # silently healthy (a refresh_token is only useful once flows
                # exist to burn it).
                relogin_required=bool(expired),
            )
        if credential_borrow_enabled(env):
            try:
                borrowed = store.get_borrowed(provider) if store is not None else None
            except Exception:
                borrowed = None
            if borrowed and borrowed.get("consented") and borrowed.get("access_token"):
                return Credential(
                    kind=KIND_BEARER,
                    value=str(borrowed["access_token"]),
                    provider=provider,
                    source=SOURCE_BORROWED,
                    health=effective_state(borrowed.get("health"), now),
                )

    # 4. no-credential providers (Ollama loopback)
    if auth_type_value == "none":
        return Credential(kind=KIND_NONE, value=None, provider=provider, source=SOURCE_NONE)

    # 5. nothing
    return None
