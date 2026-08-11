"""Declarative LLM provider registry (proposal 024, L0).

One ``ProviderSpec`` row describes a provider completely — identity, credential
shape, transport, base URL, capabilities, derivation data — so the historical
per-provider literal lists (``PROFILES``, ``PROVIDER_CONFIG``, ``DEFAULT_MODELS``,
``SCHEMA_GENERATORS``, ``native_providers``, ``_KEY_TO_PROVIDER``,
``FALLBACK_HIERARCHY``, ``_KNOWN_PROVIDERS``, ``_PREFIX_TO_PROVIDER``) become
*derivations* of this table instead of hand-maintained copies. Same move
``core/tool_capabilities.py`` made for tools in WS-2.

User-declared providers load from ``~/.polyrob/providers.yaml`` (path override:
``LLM_CUSTOM_PROVIDERS``; set-but-empty disables). Loading is lazy + cached —
this module must stay light at import (entry-point weight test) and must never
pull agents.*/aiogram/provider SDKs (layering test on ``modules.llm.profiles``).

Kill-switch: ``LLM_PROVIDER_REGISTRY`` (default ON). OFF = the legacy literal
lists are used at every seam and ``providers.yaml`` is ignored — byte-identical
to the pre-024 tree (pinned by tests/unit/modules/llm/
test_provider_registry_characterization.py in both modes).

Security invariants (proposal 024 §4.0.3, §7.1):
- user YAML may NEVER name a ``client_class_name`` — it can only select an
  existing generic transport (otherwise a file on disk is a code-exec vector);
- ``providers.yaml`` is credential-equivalent (it redirects ``base_url``) and is
  denied to agent tools via ``core/security/secret_guard.py``;
- loading is fail-open PER ENTRY: a malformed row is skipped with a WARN, never
  a crash.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


class AuthType(str, Enum):
    API_KEY = "api_key"
    OAUTH_DEVICE = "oauth_device"
    OAUTH_PKCE = "oauth_pkce"
    BORROWED = "borrowed"
    NONE = "none"          # e.g. a local Ollama loopback — no credential needed


class Transport(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"      # OpenAI-compatible /chat/completions
    ANTHROPIC_MESSAGES = "anthropic_messages"  # Anthropic /v1/messages shape
    RESPONSES = "responses"                    # OpenAI Responses API (declared; P2+)


@dataclass(frozen=True)
class OAuthSpec:
    """OAuth flow descriptor (consumed by L2; declared here so specs are complete)."""
    auth_url: str = ""
    token_url: str = ""
    client_id: str = ""
    scopes: Tuple[str, ...] = ()
    grant: str = "device_code"
    refresh_skew_sec: int = 120


@dataclass(frozen=True)
class ProviderSpec:
    # identity
    name: str
    display_name: str
    aliases: Tuple[str, ...] = ()

    # credentials
    auth_type: AuthType = AuthType.API_KEY
    env_key: Optional[str] = None            # None for OAUTH-only / NONE providers
    oauth: Optional[OAuthSpec] = None

    # transport
    transport: Transport = Transport.CHAT_COMPLETIONS
    base_url: Optional[str] = None           # None => SDK default
    base_url_env: Optional[str] = None       # operator override, e.g. OLLAMA_BASE_URL
    extra_headers: Tuple[Tuple[str, str], ...] = ()
    bearer_auth: bool = False                # ANTHROPIC_MESSAGES only: send the key as
                                             # Authorization: Bearer (z.ai) not x-api-key

    # capabilities (feed the derivations)
    supports_native_tools: bool = True
    supports_vision: bool = True
    default_model: Optional[str] = None      # None => llm_client_registry.DEFAULT_MODELS
    models: Tuple[str, ...] = ()             # declared model list (§4.0.1)
    model_prefixes: Tuple[str, ...] = ()     # feeds openai-compat prefix routing
    fallback_eligible: bool = False
    fallback_rank: Optional[int] = None      # ordering within FALLBACK_HIERARCHY
    initializable: bool = True
    client_class_name: Optional[str] = None  # None => generic client for `transport`

    # provenance
    builtin: bool = False
    subscription: bool = False               # flat-rate plan, not metered
    signup_url: Optional[str] = None
    tos_note: Optional[str] = None           # surfaced at connect time (§7.4)

    def headers(self) -> Dict[str, str]:
        return dict(self.extra_headers)

    def resolved_base_url(self, env=None) -> Optional[str]:
        """The effective base URL: ``base_url_env`` override wins, else ``base_url``."""
        env = os.environ if env is None else env
        if self.base_url_env:
            override = env.get(self.base_url_env)
            if override and str(override).strip():
                return str(override).strip()
        return self.base_url


def provider_registry_enabled(env=None) -> bool:
    """Kill-switch for the declarative registry (default ON; remove after one release)."""
    if env is not None:  # test path — value-based parse
        from core.env import parse_bool
        return parse_bool(env.get("LLM_PROVIDER_REGISTRY"), True)
    from core.env import bool_env
    return bool_env("LLM_PROVIDER_REGISTRY", True)


# ---------------------------------------------------------------------------
# Built-in specs — canonical order IS the "first provider with a key" order
# (mirrors the legacy modules/llm/profiles.py PROFILES table; fallback_rank
# mirrors the legacy LLMManager.FALLBACK_HIERARCHY order; model_prefixes mirror
# the legacy api/openai_compat/model_map._PREFIX_TO_PROVIDER table).
# ---------------------------------------------------------------------------
BUILTIN_SPECS: Tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="openrouter", display_name="OpenRouter", env_key="OPENROUTER_API_KEY",
        transport=Transport.CHAT_COMPLETIONS, base_url="https://openrouter.ai/api/v1",
        supports_native_tools=True, supports_vision=True,
        signup_url="https://openrouter.ai/", client_class_name="OpenRouterClient",
        fallback_eligible=True, fallback_rank=2, builtin=True,
    ),
    ProviderSpec(
        name="anthropic", display_name="Anthropic", env_key="ANTHROPIC_API_KEY",
        transport=Transport.ANTHROPIC_MESSAGES, base_url="https://api.anthropic.com",
        supports_native_tools=True, supports_vision=True,
        signup_url="https://console.anthropic.com/", client_class_name="AnthropicClient",
        fallback_eligible=True, fallback_rank=1, model_prefixes=("claude",), builtin=True,
    ),
    ProviderSpec(
        name="openai", display_name="OpenAI", env_key="OPENAI_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        supports_native_tools=True, supports_vision=True,
        signup_url="https://platform.openai.com/", client_class_name="OpenAIClient",
        fallback_eligible=True, fallback_rank=0, model_prefixes=("gpt", "o1", "o3"),
        builtin=True,
    ),
    ProviderSpec(
        name="gemini", display_name="Google Gemini", env_key="GEMINI_API_KEY",
        # transport is INERT for builtins with a bespoke client_class_name (the
        # factory/schema paths match on name first) — gemini actually speaks the
        # google SDK, not chat_completions. Do not trust spec.transport for a
        # builtin unless its client_class_name is None.
        transport=Transport.CHAT_COMPLETIONS,
        supports_native_tools=True, supports_vision=True,
        signup_url="https://aistudio.google.com/", client_class_name="GeminiClient",
        fallback_eligible=True, fallback_rank=3, model_prefixes=("gemini",), builtin=True,
    ),
    ProviderSpec(
        name="nvidia", display_name="NVIDIA NIM", env_key="NVIDIA_API_KEY",
        transport=Transport.CHAT_COMPLETIONS,
        base_url="https://integrate.api.nvidia.com/v1",
        supports_native_tools=True, supports_vision=False,
        signup_url="https://build.nvidia.com/", client_class_name="NvidiaClient",
        model_prefixes=("kimi",), builtin=True,
    ),
    ProviderSpec(
        name="deepseek", display_name="DeepSeek", env_key="DEEPSEEK_API_KEY",
        transport=Transport.CHAT_COMPLETIONS, base_url="https://api.deepseek.com/v1",
        supports_native_tools=False, supports_vision=False,
        signup_url="https://platform.deepseek.com/", client_class_name="DeepSeekClient",
        # Direct client disabled (tool-calling broken) — reach DeepSeek via OpenRouter.
        initializable=False, model_prefixes=("deepseek",), builtin=True,
    ),
)


# ---------------------------------------------------------------------------
# User-declared providers (~/.polyrob/providers.yaml)
# ---------------------------------------------------------------------------
# Fields a providers.yaml row may set. client_class_name / oauth / builtin /
# fallback_rank are deliberately absent (code-selection and internal ordering
# are never user data).
_USER_SETTABLE_FIELDS = frozenset({
    "display_name", "aliases", "auth_type", "env_key", "transport", "base_url",
    "base_url_env", "extra_headers", "bearer_auth", "supports_native_tools",
    "supports_vision", "default_model", "models", "model_prefixes",
    "fallback_eligible", "initializable", "subscription", "signup_url", "tos_note",
})

_GENERIC_CLIENT_FOR_TRANSPORT = {
    Transport.CHAT_COMPLETIONS: "OpenAICompatClient",
    Transport.ANTHROPIC_MESSAGES: "AnthropicCompatClient",
    # Transport.RESPONSES: declared but unimplemented (P2) — no generic client yet.
}


def generic_client_class_name(transport: Transport) -> Optional[str]:
    """The generic client class serving *transport*, or None if unimplemented."""
    return _GENERIC_CLIENT_FOR_TRANSPORT.get(transport)


#: Placeholder api_key for AuthType.NONE providers (Ollama & friends): the
#: OpenAI SDK requires a non-empty key, and LLMManager's bootstrap gates treat
#: a missing api_key as "not configured" — the sentinel satisfies both. Never
#: a real credential.
NO_KEY_SENTINEL = "not-needed"


def user_providers_path(env=None) -> Optional[str]:
    """Path to the user providers file, or None when disabled.

    ``LLM_CUSTOM_PROVIDERS`` unset => ``<polyrob_home>/providers.yaml``;
    set-but-empty => disabled; otherwise the given path.
    """
    env = os.environ if env is None else env
    raw = env.get("LLM_CUSTOM_PROVIDERS")
    if raw is not None:
        raw = str(raw).strip()
        return raw or None
    from core.paths import polyrob_home
    return str(polyrob_home() / "providers.yaml")


def _coerce_str_tuple(value) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


# --- providers.yaml row validation (024 security review C1, 2026-08-07) -----
# A row's env_key/base_url are what the generic clients SEND CREDENTIALS to —
# an unvalidated row could name any process secret (WALLET_MASTER_SEED,
# TELEGRAM_BOT_TOKEN, ...) as its "api key" and ship it to an arbitrary URL.
# env_key must therefore be API-key-shaped, base_url_env URL-shaped, and both
# must avoid known non-LLM secret name tokens; base_url must be http(s) and
# never the cloud metadata endpoint. A violating row is SKIPPED with a WARN
# (loud), never silently narrowed.
import re as _re

_ENV_KEY_RE = _re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")
_BASE_URL_ENV_RE = _re.compile(r"^[A-Z][A-Z0-9_]*_URL$")
_SENSITIVE_NAME_TOKENS = (
    "SEED", "MNEMONIC", "WALLET", "PRIVATE", "SECRET", "PASSWORD", "PASSWD",
    "JWT", "TELEGRAM", "TWITTER", "X402", "SMTP", "IMAP", "OAUTH", "MASTER",
    "COOKIE", "SESSION",
)


def _env_name_sensitive(name: str) -> bool:
    upper = name.upper()
    return any(tok in upper for tok in _SENSITIVE_NAME_TOKENS)


def _validate_row_security(name: str, kwargs: Dict[str, object]) -> Optional[str]:
    """Return a rejection reason for a hostile/malformed row, or None if OK."""
    env_key = kwargs.get("env_key")
    if env_key is not None:
        if not _ENV_KEY_RE.match(str(env_key)) or _env_name_sensitive(str(env_key)):
            return (
                f"env_key {env_key!r} is not an API-key-shaped variable name "
                "(must match *_API_KEY and not name a non-LLM secret)"
            )
    base_url_env = kwargs.get("base_url_env")
    if base_url_env is not None:
        if not _BASE_URL_ENV_RE.match(str(base_url_env)) or _env_name_sensitive(str(base_url_env)):
            return (
                f"base_url_env {base_url_env!r} is not a URL-shaped variable name "
                "(must match *_URL and not name a secret)"
            )
    base_url = kwargs.get("base_url")
    if base_url is not None:
        reason = _validate_base_url(str(base_url))
        if reason:
            return reason
    return None


def _validate_base_url(url: str) -> Optional[str]:
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return f"base_url unparseable ({exc})"
    if parts.scheme not in ("http", "https"):
        return f"base_url scheme {parts.scheme!r} not allowed (http/https only)"
    host = (parts.hostname or "").lower()
    if not host:
        return "base_url has no host"
    if host == "metadata.google.internal" or host.startswith("169.254."):
        return f"base_url host {host!r} is a cloud-metadata endpoint"
    return None


# Queryable load state for the last providers.yaml parse — what `polyrob
# doctor` renders as the "providers file" block (UX assessment 2026-08-07, Q7).
# Shape: {"path", "loaded": [names], "rejected": [(name, reason)], "file_error"}.
# Empty dict = no file configured/present. Reset with the registry cache.
_LOAD_REPORT: Dict[str, object] = {}


def _reject(name: str, reason: str) -> None:
    """Log a row rejection AND record it in the load report (never log-only)."""
    logger.warning("providers.yaml[%s]: %s — row skipped", name, reason)
    _LOAD_REPORT.setdefault("rejected", []).append((str(name), reason))


def user_providers_report() -> Dict[str, object]:
    """The last providers.yaml load outcome (path, loaded, rejected, file_error).

    Ensures the registry has loaded first; empty dict when no file is
    configured/present or the registry is disabled. Consumed by
    ``polyrob doctor`` so rejection feedback is a queryable state, not a
    transient load-time log line.
    """
    get_specs()
    return dict(_LOAD_REPORT)


def _parse_user_row(name: str, row: dict, base: Optional[ProviderSpec]) -> Optional[ProviderSpec]:
    """Build one spec from a providers.yaml row. Returns None (with a WARN +
    load-report entry) on a malformed row — fail-open per entry, never a crash."""
    if not isinstance(row, dict):
        _reject(str(name), "entry is not a mapping")
        return None
    name = str(name).strip().lower()
    if not name or any(c in name for c in "/\\ \t") or name.startswith("."):
        _reject(name, f"invalid provider name {name!r}")
        return None
    if name.endswith("_client"):
        # llm_manager derives provider names by stripping '_client' from service
        # names — a provider literally named '*_client' would alias into another
        # provider's service slot (review M7).
        _reject(name, "provider name may not end in '_client'")
        return None

    kwargs: Dict[str, object] = {}
    for key, value in row.items():
        if key == "client_class_name":
            logger.warning(
                "providers.yaml[%s]: 'client_class_name' is not user-settable "
                "(user rows may only select a generic transport) — ignored", name
            )
            continue
        if key not in _USER_SETTABLE_FIELDS:
            logger.warning("providers.yaml[%s]: unknown field %r — ignored", name, key)
            continue
        try:
            if key == "auth_type":
                kwargs[key] = AuthType(str(value).strip().lower())
            elif key == "transport":
                kwargs[key] = Transport(str(value).strip().lower())
            elif key in ("aliases", "models", "model_prefixes"):
                kwargs[key] = _coerce_str_tuple(value)
            elif key == "extra_headers":
                if not isinstance(value, dict):
                    raise ValueError("extra_headers must be a mapping")
                kwargs[key] = tuple((str(k), str(v)) for k, v in value.items())
            elif key in ("supports_native_tools", "supports_vision", "bearer_auth",
                         "fallback_eligible", "initializable", "subscription"):
                kwargs[key] = bool(value)
            else:
                kwargs[key] = None if value is None else str(value)
        except (ValueError, TypeError) as exc:
            _reject(name, f"bad {key} ({exc})")
            return None

    reason = _validate_row_security(name, kwargs)
    if reason:
        _reject(name, reason)
        return None

    if base is not None:
        # Override of a built-in provider: user fields replace the built-in's,
        # but the client class / builtin provenance / fallback rank are kept.
        # transport is bound to the built-in's bespoke client — never movable.
        if "transport" in kwargs:
            logger.warning(
                "providers.yaml[%s]: 'transport' cannot be overridden on a "
                "built-in provider — ignored", name
            )
            kwargs.pop("transport")
        return replace(base, **kwargs)

    spec = ProviderSpec(name=name,
                        display_name=str(kwargs.pop("display_name", None) or name),
                        builtin=False, client_class_name=None, **kwargs)
    if generic_client_class_name(spec.transport) is None:
        _reject(name, f"transport {spec.transport.value!r} has no generic client yet")
        return None
    if not spec.base_url and not spec.base_url_env:
        # Without an endpoint the generic client would ride its DEFAULT base URL
        # (silent traffic misdirection) — refuse the row (assessment Q14).
        _reject(name, "no base_url (or base_url_env) declared")
        return None
    if not spec.models:
        logger.warning(
            "providers.yaml[%s]: no 'models:' declared — usable only when named "
            "explicitly (-p %s -m <model>); absent from model listings", name, name
        )
    return spec


def _load_user_specs() -> Tuple[ProviderSpec, ...]:
    """Parse providers.yaml (lazy; caller caches). Fail-open on every error."""
    global _LOAD_REPORT
    _LOAD_REPORT = {}
    path = user_providers_path()
    if not path or not os.path.isfile(path):
        return ()
    _LOAD_REPORT = {"path": path, "loaded": [], "rejected": [], "file_error": None}
    # providers.yaml is credential-equivalent (it redirects the agent's own
    # inference endpoint). The secret_guard denial is scoped to the polyrob
    # config homes — warn loudly when the operator relocates it outside them,
    # where agent file tools could reach it.
    parts = {p.lower() for p in os.path.normpath(path).split(os.sep)}
    if ".polyrob" not in parts and ".rob" not in parts:
        logger.warning(
            "LLM_CUSTOM_PROVIDERS points outside a polyrob config home (%s). "
            "This file redirects inference endpoints — keep it under ~/.polyrob "
            "so agent file tools are denied access to it.", path
        )
    try:
        # Group/world-writable providers.yaml = anyone on the box can redirect
        # the agent's inference endpoint (review M6) — refuse to load it.
        mode = os.stat(path).st_mode
        if mode & 0o022:
            logger.warning(
                "providers.yaml %s is group/world-writable (mode %o) — refusing "
                "to load it; chmod 600 to enable", path, mode & 0o777
            )
            _LOAD_REPORT["file_error"] = (
                f"group/world-writable (mode {mode & 0o777:o}) — refusing to load; chmod 600"
            )
            return ()
    except OSError:
        return ()
    try:
        import yaml  # lazy: keep module import light (entry-point weight test)
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("providers.yaml unreadable (%s) — no user providers loaded", exc)
        _LOAD_REPORT["file_error"] = f"unreadable ({exc}) — no user providers loaded"
        return ()
    rows = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        if rows is not None:
            logger.warning("providers.yaml: top-level 'providers' must be a mapping")
            _LOAD_REPORT["file_error"] = "top-level 'providers' must be a mapping"
        return ()
    builtin_by_name = {s.name: s for s in BUILTIN_SPECS}
    out: List[ProviderSpec] = []
    for name, row in rows.items():
        spec = _parse_user_row(name, row, builtin_by_name.get(str(name).strip().lower()))
        if spec is not None:
            # Make every endpoint redirect VISIBLE — a silent redirect is the
            # threat model here (review C1 fix #4).
            logger.info(
                "provider registry: user row %r → base_url=%s env_key=%s transport=%s",
                spec.name, spec.resolved_base_url(), spec.env_key, spec.transport.value,
            )
            out.append(spec)
            _LOAD_REPORT.setdefault("loaded", []).append(spec.name)
    return tuple(out)


# ---------------------------------------------------------------------------
# The merged registry (cached; reset via reset_provider_registry_cache)
# ---------------------------------------------------------------------------
_SPECS_CACHE: Optional[Tuple[ProviderSpec, ...]] = None


def get_specs() -> Tuple[ProviderSpec, ...]:
    """All provider specs, canonical order: built-ins (legacy PROFILES order,
    user overrides applied in place), then new user-declared providers.

    With ``LLM_PROVIDER_REGISTRY`` off, returns the built-ins only (the seams
    also fall back to their legacy literals — see the kill-switch contract).
    """
    global _SPECS_CACHE
    if _SPECS_CACHE is not None:
        return _SPECS_CACHE
    if not provider_registry_enabled():
        _SPECS_CACHE = BUILTIN_SPECS
        return _SPECS_CACHE
    user = _load_user_specs()
    overrides = {s.name: s for s in user if any(b.name == s.name for b in BUILTIN_SPECS)}
    merged: List[ProviderSpec] = [overrides.get(b.name, b) for b in BUILTIN_SPECS]
    merged.extend(s for s in user if s.name not in overrides)
    _SPECS_CACHE = tuple(merged)
    return _SPECS_CACHE


def get_spec(name: str) -> Optional[ProviderSpec]:
    """Spec by canonical name or alias (case-insensitive); None if unknown."""
    if not name:
        return None
    low = str(name).strip().lower()
    for spec in get_specs():
        if spec.name == low or low in spec.aliases:
            return spec
    return None


def user_declared_specs() -> Tuple[ProviderSpec, ...]:
    return tuple(s for s in get_specs() if not s.builtin)


def reset_provider_registry_cache() -> None:
    """Clear every registry-derived cache (test isolation / config reload)."""
    global _SPECS_CACHE, _LOAD_REPORT
    _SPECS_CACHE = None
    _LOAD_REPORT = {}
    # Downstream derivation caches (lazy imports: no hard coupling, no cycles).
    try:
        from modules.llm import profiles
        profiles.PROFILES.reset()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        from modules.llm import llm_client_registry
        llm_client_registry.AVAILABLE_MODELS.reset()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        from modules.llm import model_registry
        model_registry.PROVIDER_CONFIG._config = None
    except Exception:
        pass
