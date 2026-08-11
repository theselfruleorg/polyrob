"""Declarative provider profiles (roadmap P8, Reference §28; proposal 024 L0).

A ``ProviderProfile`` *describes* a provider — identity, auth, base URL, default
model, capability flags — and owns no client construction, credential rotation, or
streaming. Since proposal 024 the profile table is a *derivation* of the
``ProviderSpec`` registry (``modules/llm/provider_spec.py``), which also carries
user-declared providers from ``~/.polyrob/providers.yaml``. The legacy literal
table is kept as the ``LLM_PROVIDER_REGISTRY=off`` kill-switch path (byte-identical;
remove with the kill-switch after one release).

The default model deliberately reads from ``llm_client_registry.DEFAULT_MODELS`` so
the default-model *policy* stays single-sourced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional

from modules.llm.llm_client_registry import get_default_model


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    display_name: str
    env_key: str                      # API-key environment variable ("" = none)
    auth_type: str = "api_key"        # api_key | oauth_* | borrowed | none
    base_url: Optional[str] = None    # None => provider SDK default
    supports_native_tools: bool = True
    supports_vision: bool = True
    signup_url: Optional[str] = None
    initializable: bool = True        # False = key alone can't bootstrap a client
                                      # (deepseek: direct client disabled → use OpenRouter)

    @property
    def default_model(self) -> str:
        """The default model for this provider (single-sourced from the registry)."""
        return get_default_model(self.name)


# NOTE: insertion order IS the canonical preference order for "first provider with
# a key" (see ``providers_with_keys`` / ``core.runtime_config``). OpenRouter is FIRST
# (2026-06-24): it is the preferred default client whenever its key is present —
# explicit ``-p`` and operator pins (DEFAULT_PROVIDER/CHAT_PROVIDER) still win.
#
# Kill-switch path (LLM_PROVIDER_REGISTRY=off) ONLY — the live table is derived
# from provider_spec.get_specs(). Keep in lockstep with BUILTIN_SPECS (pinned by
# the characterization suite in both modes) until the kill-switch is removed.
_LEGACY_PROFILES: Dict[str, ProviderProfile] = {
    "openrouter": ProviderProfile(
        name="openrouter", display_name="OpenRouter", env_key="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1", supports_native_tools=True,
        supports_vision=True, signup_url="https://openrouter.ai/",
    ),
    "anthropic": ProviderProfile(
        name="anthropic", display_name="Anthropic", env_key="ANTHROPIC_API_KEY",
        base_url="https://api.anthropic.com", supports_native_tools=True,
        supports_vision=True, signup_url="https://console.anthropic.com/",
    ),
    "openai": ProviderProfile(
        name="openai", display_name="OpenAI", env_key="OPENAI_API_KEY",
        supports_native_tools=True, supports_vision=True,
        signup_url="https://platform.openai.com/",
    ),
    "gemini": ProviderProfile(
        name="gemini", display_name="Google Gemini", env_key="GEMINI_API_KEY",
        supports_native_tools=True, supports_vision=True,
        signup_url="https://aistudio.google.com/",
    ),
    "nvidia": ProviderProfile(
        name="nvidia", display_name="NVIDIA NIM", env_key="NVIDIA_API_KEY",
        base_url="https://integrate.api.nvidia.com/v1", supports_native_tools=True,
        supports_vision=False, signup_url="https://build.nvidia.com/",
    ),
    "deepseek": ProviderProfile(
        name="deepseek", display_name="DeepSeek", env_key="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/v1", supports_native_tools=False,
        supports_vision=False, signup_url="https://platform.deepseek.com/",
        # Direct client disabled (tool-calling broken) — reach DeepSeek via OpenRouter
        # (OPENROUTER_API_KEY + model deepseek/deepseek-chat). A DEEPSEEK_API_KEY alone
        # cannot bootstrap the agent, so it must NOT count toward "has a usable key".
        initializable=False,
    ),
}


def _profiles_from_specs() -> Dict[str, ProviderProfile]:
    """Derive the profile view from the ProviderSpec registry (proposal 024)."""
    from modules.llm.provider_spec import get_specs
    out: Dict[str, ProviderProfile] = {}
    for s in get_specs():
        out[s.name] = ProviderProfile(
            name=s.name,
            display_name=s.display_name,
            env_key=s.env_key or "",
            auth_type=s.auth_type.value,
            base_url=s.resolved_base_url(),
            supports_native_tools=s.supports_native_tools,
            supports_vision=s.supports_vision,
            signup_url=s.signup_url,
            initializable=s.initializable,
        )
    return out


class _LazyProfiles:
    """Read-only dict-like view over the provider registry.

    Built on first access (keeps module import light: no YAML/file I/O at import,
    per tests/test_import_layering.py) and cached; ``reset()`` clears the snapshot
    (called by ``provider_spec.reset_provider_registry_cache``).
    """

    def __init__(self) -> None:
        self._snapshot: Optional[Dict[str, ProviderProfile]] = None

    def _ensure(self) -> Dict[str, ProviderProfile]:
        if self._snapshot is None:
            from modules.llm.provider_spec import provider_registry_enabled
            if provider_registry_enabled():
                self._snapshot = _profiles_from_specs()
            else:
                self._snapshot = dict(_LEGACY_PROFILES)
        return self._snapshot

    def reset(self) -> None:
        self._snapshot = None

    # read-only mapping interface (order-preserving)
    def __getitem__(self, key: str) -> ProviderProfile:
        return self._ensure()[key]

    def __contains__(self, key: object) -> bool:
        return key in self._ensure()

    def __iter__(self) -> Iterator[str]:
        return iter(self._ensure())

    def __len__(self) -> int:
        return len(self._ensure())

    def __bool__(self) -> bool:
        return bool(self._ensure())

    def get(self, key: str, default=None):
        return self._ensure().get(key, default)

    def keys(self):
        return self._ensure().keys()

    def values(self):
        return self._ensure().values()

    def items(self):
        return self._ensure().items()


PROFILES = _LazyProfiles()


def providers_with_keys(env=None) -> List[str]:
    """Return provider names whose API key is present in *env*, in PROFILES order.

    This is the single source of truth for "which LLM providers have a key" — it
    replaces the divergent per-call-site key lists across the CLI (config_store,
    chat.py, doctor.py, model.py, banner.py). The returned order IS the canonical
    preference order for "first provider with a key".

    *env* defaults to ``os.environ``; pass a mapping for testability. A blank value
    counts as absent.
    """
    import os
    env = os.environ if env is None else env
    return [p.name for p in PROFILES.values() if p.env_key and env.get(p.env_key)]


def _keyless_by_design(p: ProviderProfile) -> bool:
    """True for a provider that needs no credential at all (``auth_type: none``,
    e.g. a local Ollama declared in providers.yaml). Such providers count as
    usable/initializable with no env key — the client sends a sentinel instead
    (``provider_spec.NO_KEY_SENTINEL``). Without this, a keyless box with a
    working local endpoint was refused by every no-key gate (B2, 2026-08-07).
    """
    return p.initializable and not p.env_key and p.auth_type == "none"


def initializable_providers_with_keys(env=None) -> List[str]:
    """``providers_with_keys`` restricted to providers a key ALONE can bootstrap.

    This is the SSOT for "does the user have a *usable* provider key" — the gating
    oracle that ``should_warn_no_key`` / the runtime resolver / the env-backfill and
    ``LLMManager._initialize``'s ``clients_to_try`` all derive from, so they can never
    disagree. Excludes deepseek (direct client disabled — route via OpenRouter).
    Includes keyless-by-design (``auth_type: none``) providers unconditionally. The
    raw ``providers_with_keys`` stays the DISPLAY oracle (doctor/webview show a key is
    present even when it can't bootstrap directly).
    """
    import os
    env = os.environ if env is None else env
    return [
        p.name for p in PROFILES.values()
        if _keyless_by_design(p)
        or (p.env_key and env.get(p.env_key) and p.initializable)
    ]


# Placeholder values that are "present" but not a real key (mirrors
# core.config.BotConfig.validate_api_keys, generalized across providers).
_PLACEHOLDER_KEYS = {
    "your-openai-key", "your-anthropic-key", "your-api-key", "your-alchemy-key",
    "your-gemini-key", "your-openrouter-key", "your-key", "changeme", "none", "null",
}
# Real provider API keys are comfortably longer than this; BotConfig blanks shorter
# ones for openai/anthropic. Every provider's real key (gemini ~39 is the shortest)
# clears 20, so the bar is safe to apply across providers.
_MIN_KEY_LEN = 20


def looks_like_real_key(value) -> bool:
    """True when *value* is a plausibly-real API key (not blank / placeholder / stub).

    Mirrors ``BotConfig.validate_api_keys`` so the GATING oracles agree with what the
    LLM manager will actually accept — a malformed key must NOT pass a guard only to be
    blanked by BotConfig and crash the manager with a misleading 'No API key found'.
    """
    if not value:
        return False
    v = str(value).strip()
    if v.lower() in _PLACEHOLDER_KEYS:
        return False
    return len(v) >= _MIN_KEY_LEN


def usable_providers_with_keys(env=None) -> List[str]:
    """Initializable providers whose key VALUE is well-formed (mirrors BotConfig).

    THE gating oracle wherever real key values are available (``should_warn_no_key``,
    the env-backfill, config-store resolution). Stricter than
    ``initializable_providers_with_keys`` (which is presence-only, for the resolver's
    name-based path) — it also rejects placeholder / too-short values. Keyless-by-
    design (``auth_type: none``) providers are always usable. Raw
    ``providers_with_keys`` remains the DISPLAY oracle.
    """
    import os
    env = os.environ if env is None else env
    return [
        p.name for p in PROFILES.values()
        if _keyless_by_design(p)
        or (p.initializable and p.env_key and looks_like_real_key(env.get(p.env_key)))
    ]


def no_key_message() -> str:
    """The single canonical no-key message (neutral module — no ``cli`` import).

    Re-exported from ``cli.keys`` and reused by ``LLMManager._initialize``'s raise so
    every no-key surface says the same thing. The provider list derives from the
    spec registry (NOT a literal), so a user-declared providers.yaml row's env key
    is named too — the message must never deny the user's own configuration.
    """
    parts = []
    for p in PROFILES.values():
        if not p.env_key:
            continue  # keyless-by-design rows have no key to set
        label = p.env_key
        if p.name == "openrouter":
            label += " (recommended)"
        if not p.initializable:
            label += (
                " (direct client disabled — use OPENROUTER_API_KEY with model "
                "deepseek/deepseek-chat)"
            )
        parts.append(label)
    return (
        "No API key found. Run `polyrob init` to set one up, or put a provider key in "
        "any of: process env, ./.polyrob/.env, ~/.polyrob/.env, root .env, "
        "config/.env.development, or config/.env.production.\n"
        "Supported providers: " + ", ".join(parts) + ".\n"
        "Keyless/custom endpoints (a local Ollama, vLLM, a corporate gateway) need no "
        "key at all — declare them in ~/.polyrob/providers.yaml (see the configuration "
        "guide)."
    )


def get_profile(name: str) -> Optional[ProviderProfile]:
    return PROFILES.get(name)


def all_profiles() -> List[ProviderProfile]:
    return list(PROFILES.values())


def extra_llm_config_blocks(env=None) -> Dict[str, Dict[str, object]]:
    """Per-provider LLM-config additions from the ProviderSpec registry (024 seam 2).

    Consumed by ``core.config.BotConfig.get_llm_config()`` through the existing,
    layering-allowlisted ``core → modules.llm.profiles`` edge. Returns:

    - a full ``{"api_key", "base_url", "auth_type"}`` block for every user-declared
      (non-builtin) provider, so ``create_llm_client``/``LLMManager`` can bootstrap
      it exactly like a built-in; and
    - a ``{"base_url": ...}`` override for a BUILT-IN provider whose effective base
      URL was redirected via providers.yaml / its ``base_url_env`` — merged into the
      literal block so clients that read config (OpenAIClient) see it.

    Empty (byte-identical config) when the registry is off or no user file exists.
    Fail-open: any registry error yields ``{}`` — config building must never break.
    """
    import os
    env = os.environ if env is None else env
    try:
        from modules.llm.provider_spec import (
            BUILTIN_SPECS,
            get_specs,
            provider_registry_enabled,
        )
        if not provider_registry_enabled():
            return {}
        out: Dict[str, Dict[str, object]] = {}
        builtin_defaults = {b.name: b for b in BUILTIN_SPECS}
        for s in get_specs():
            if s.builtin:
                default = builtin_defaults.get(s.name)
                if default is None:
                    continue
                resolved = s.resolved_base_url(env)
                if resolved != default.base_url:
                    out.setdefault(s.name, {})["base_url"] = resolved
                # An env_key override on a built-in (corporate gateway with its
                # own key var) must also re-source the api_key — the BotConfig
                # pydantic field keeps reading the ORIGINAL var otherwise.
                if s.env_key and s.env_key != default.env_key and env.get(s.env_key):
                    out.setdefault(s.name, {})["api_key"] = env.get(s.env_key)
            else:
                key = env.get(s.env_key) if s.env_key else None
                if not key and s.auth_type.value == "none":
                    # AuthType.NONE (Ollama & friends): the manager's bootstrap
                    # gates skip providers with no api_key — the sentinel makes
                    # a keyless local endpoint bootstrappable (024 live-path fix).
                    from modules.llm.provider_spec import NO_KEY_SENTINEL
                    key = NO_KEY_SENTINEL
                out[s.name] = {
                    "api_key": key,
                    "base_url": s.resolved_base_url(env),
                    "auth_type": s.auth_type.value,
                }
        return out
    except Exception:
        return {}
