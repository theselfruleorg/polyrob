"""Declarative provider profiles (roadmap P8, Reference §28).

A ``ProviderProfile`` *describes* a provider — identity, auth, base URL, default
model, capability flags — and owns no client construction, credential rotation, or
streaming. Today POLYROB's ``modules/llm/*_client.py`` files conflate profile +
transport + adapter; this layer extracts the declarative half so it has a single
home. It is purely additive: nothing is rewired to consume it yet (that migration
is the rest of P8), so behavior is unchanged.

The default model deliberately reads from ``llm_client_registry.DEFAULT_MODELS`` so
the default-model *policy* stays single-sourced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from modules.llm.llm_client_registry import get_default_model


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    display_name: str
    env_key: str                      # API-key environment variable
    auth_type: str = "api_key"        # api_key | oauth | aws
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
PROFILES: Dict[str, ProviderProfile] = {
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
    return [p.name for p in PROFILES.values() if env.get(p.env_key)]


def initializable_providers_with_keys(env=None) -> List[str]:
    """``providers_with_keys`` restricted to providers a key ALONE can bootstrap.

    This is the SSOT for "does the user have a *usable* provider key" — the gating
    oracle that ``should_warn_no_key`` / the runtime resolver / the env-backfill and
    ``LLMManager._initialize``'s ``clients_to_try`` all derive from, so they can never
    disagree. Excludes deepseek (direct client disabled — route via OpenRouter). The
    raw ``providers_with_keys`` stays the DISPLAY oracle (doctor/webview show a key is
    present even when it can't bootstrap directly).
    """
    import os
    env = os.environ if env is None else env
    return [p.name for p in PROFILES.values() if env.get(p.env_key) and p.initializable]


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
    name-based path) — it also rejects placeholder / too-short values. Raw
    ``providers_with_keys`` remains the DISPLAY oracle.
    """
    import os
    env = os.environ if env is None else env
    return [
        p.name for p in PROFILES.values()
        if p.initializable and looks_like_real_key(env.get(p.env_key))
    ]


def no_key_message() -> str:
    """The single canonical no-key message (neutral module — no ``cli`` import).

    Re-exported from ``cli.keys`` and reused by ``LLMManager._initialize``'s raise so
    every no-key surface says the same thing.
    """
    return (
        "No API key found. Run `polyrob init` to set one up, or put a provider key in "
        "any of: process env, ./.polyrob/.env, ~/.polyrob/.env, root .env, "
        "config/.env.development, or config/.env.production.\n"
        "Supported providers: OPENROUTER_API_KEY (recommended), ANTHROPIC_API_KEY, "
        "OPENAI_API_KEY, GEMINI_API_KEY, NVIDIA_API_KEY, DEEPSEEK_API_KEY "
        "(direct client disabled — use OPENROUTER_API_KEY with model deepseek/deepseek-chat)."
    )


def get_profile(name: str) -> Optional[ProviderProfile]:
    return PROFILES.get(name)


def all_profiles() -> List[ProviderProfile]:
    return list(PROFILES.values())
