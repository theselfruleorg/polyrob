"""Generic, spec-parameterized LLM clients (proposal 024, L0).

Two clients carry every ProviderSpec that names no bespoke class:

- ``OpenAICompatClient`` — any OpenAI-compatible ``/chat/completions`` endpoint
  (Ollama, LM Studio, vLLM, SGLang, llama.cpp, LiteLLM, Groq, Together,
  Fireworks, corporate gateways). Rides ``OpenRouterClient``'s transport with
  credentials/base URL/headers taken from the spec — the same subclass move
  ``NvidiaClient`` already proved out, parameterized instead of hardcoded.
- ``AnthropicCompatClient`` — any Anthropic-``/v1/messages``-compatible endpoint
  reached with a non-Anthropic credential (z.ai GLM Coding Plan; later a Claude
  subscription OAuth bearer). Rides ``AnthropicClient``.

Both derive their provider name from the service name (``{provider}_client`` —
the convention ``create_llm_client`` uses) and read their config block from
``BotConfig.get_llm_config()`` (populated for user-declared providers via
``profiles.extra_llm_config_blocks``).

This module is imported lazily (inside ``create_llm_client``) so the provider
SDKs it drags in never load at CLI entry-point import time. ⚠️ 058 T7.2:
``AnthropicCompatClient`` lives in ``modules/llm/compat_anthropic.py`` because
its base class imports the ``anthropic`` SDK at module level — an extra since
T1.4 — and an OpenAI-compatible deployment must not need it. The name is still
importable from here (module ``__getattr__``) for back-compat.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from modules.llm.llm_client import LLMClient
from modules.llm.llm_client_registry import get_default_model
from modules.llm.model_registry import get_model_config
from modules.llm.openrouter_client import OpenRouterClient
from modules.llm.provider_spec import (
    NO_KEY_SENTINEL as _NO_KEY_SENTINEL,
    AuthType,
    ProviderSpec,
    get_spec,
)
from core.config import BotConfig
from core.exceptions import ServiceError


def _provider_from_service_name(name: str) -> str:
    return name[: -len("_client")] if name.endswith("_client") else name


def _spec_for(name: str) -> ProviderSpec:
    provider = _provider_from_service_name(name)
    spec = get_spec(provider)
    if spec is None:
        raise ServiceError(f"No ProviderSpec registered for '{provider}'")
    return spec


def _resolve_key(spec: ProviderSpec, cfg: Dict) -> Optional[str]:
    """The credential for *spec*: config block first, then its env-var chain.

    The chain matters — a provider that publishes several names (z.ai's
    GLM_API_KEY / ZAI_API_KEY / Z_AI_API_KEY) must not fail to construct just
    because the user set the alias rather than the primary.
    """
    key = cfg.get("api_key")
    if not key:
        _name, key = spec.resolve_env_key()
    if not key and spec.auth_type is AuthType.NONE:
        key = _NO_KEY_SENTINEL
    return key


class OpenAICompatClient(OpenRouterClient):
    """OpenAI-compatible chat-completions client driven by a ProviderSpec."""

    def __init__(self, config: BotConfig, name: str):
        # Skip OpenRouterClient.__init__ (it reads the 'openrouter' config block);
        # call the grandparent LLMClient initializer directly (NvidiaClient pattern).
        LLMClient.__init__(self, config=config, name=name)
        self._client = None
        self._spec = _spec_for(name)
        self._PROVIDER_LABEL = self._spec.display_name

        cfg = config.get_llm_config().get(self._spec.name, {}) or {}
        self.api_key = _resolve_key(self._spec, cfg)
        self.model_type = cfg.get("model") or get_default_model(self._spec.name)
        self.last_response = None

        # OpenRouter attribution headers don't apply; spec headers ride
        # _get_openrouter_headers below.
        self.site_url = ""
        self.site_name = ""

        model_config = get_model_config(self.model_type)
        if model_config:
            self.max_tokens = model_config.max_completion_tokens or 32768
        else:
            self.max_tokens = 32768
            self.logger.info(
                f"Model '{self.model_type}' not in registry — using defaults "
                f"(declare it under 'models:' in providers.yaml to list it)"
            )
        self.supports_vision = self._resolve_supports_vision()
        self.temperature = 0.7

    def _resolve_supports_vision(self, model_type: Optional[str] = None) -> bool:
        # Registry wins; an unregistered model falls back to the spec's declared
        # capability (not the base class's blanket True).
        mc = get_model_config(model_type or self.model_type)
        if mc:
            return mc.capabilities.supports_vision
        return self._spec.supports_vision

    def _supports_tools(self) -> bool:
        if not self._spec.supports_native_tools:
            return False
        return super()._supports_tools()

    def _profile_base_url(self) -> Optional[str]:
        # api_key is passed so a key-prefix rule can redirect the host
        # (a Kimi Code key must not go to the legacy platform endpoint).
        return self._spec.resolved_base_url(api_key=self.api_key)

    def _validate_llm_config(self) -> None:
        if self._spec.auth_type is AuthType.NONE:
            return
        if not self.api_key or self.api_key == _NO_KEY_SENTINEL:
            hint = f" (set {self._spec.env_key})" if self._spec.env_key else ""
            raise ServiceError(f"{self._spec.display_name} API key not provided{hint}")

    async def _setup_client(self) -> None:
        from openai import AsyncOpenAI
        kwargs = {"api_key": self.api_key or _NO_KEY_SENTINEL}
        base_url = self._profile_base_url()
        if base_url:
            kwargs["base_url"] = base_url
        headers = self._spec.headers()
        if headers:
            kwargs["default_headers"] = headers
        self._client = AsyncOpenAI(**kwargs)

    def _get_openrouter_headers(self) -> Dict[str, str]:
        return self._spec.headers()


def __getattr__(name: str):
    # Back-compat: `from modules.llm.compat_clients import AnthropicCompatClient`
    # still works, but only resolves (and imports the SDK) when asked for.
    if name == "AnthropicCompatClient":
        from modules.llm.compat_anthropic import AnthropicCompatClient
        return AnthropicCompatClient
    raise AttributeError(name)
