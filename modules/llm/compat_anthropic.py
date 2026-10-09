"""``AnthropicCompatClient`` — any Anthropic-``/v1/messages``-compatible endpoint
reached with a non-Anthropic credential (z.ai GLM Coding Plan; later a Claude
subscription OAuth bearer). Rides ``AnthropicClient`` (proposal 024, L0).

Split out of ``compat_clients.py`` (058 T7.2): ``AnthropicClient`` imports the
``anthropic`` SDK at module level, and since T1.4 that SDK is an extra. Keeping
this class beside ``OpenAICompatClient`` made every OpenAI-compatible provider
(Ollama, Groq, LM Studio, …) drag the Anthropic SDK in. ``create_llm_client``
calls ``core.lazy_deps.ensure_provider("provider.anthropic")`` before importing
this module.
"""
from __future__ import annotations

from typing import Optional

from modules.llm.anthropic_client import AnthropicClient
from modules.llm.compat_clients import _NO_KEY_SENTINEL, _resolve_key, _spec_for
from modules.llm.llm_client import LLMClient
from modules.llm.llm_client_registry import get_default_model
from modules.llm.model_registry import get_model_config
from modules.llm.provider_spec import AuthType
from core.config import BotConfig
from core.exceptions import ServiceError


class AnthropicCompatClient(AnthropicClient):
    """Anthropic-messages-compatible client driven by a ProviderSpec."""

    # F4: never send a ``cache_control.ttl`` to a third-party /v1/messages
    # validator — an unknown key there is a 4xx on the request itself, not a
    # lost optimisation (same class as the 422 in usage_extract.py:48-50).
    _SUPPORTS_CACHE_TTL = False

    # F9: same reasoning for `defer_loading` / `tool_addition` — a third-party
    # /v1/messages validator does not know the mid-conversation-tool-changes
    # beta, and an unknown key there is a 4xx on the request itself.
    _SUPPORTS_DEFERRED_TOOLS = False

    def __init__(self, config: BotConfig, name: str):
        # Skip AnthropicClient.__init__ (it reads the 'anthropic' config block);
        # call the grandparent LLMClient initializer directly.
        LLMClient.__init__(self, config=config, name=name)
        self._client = None
        self._spec = _spec_for(name)
        self._PROVIDER_LABEL = self._spec.display_name

        cfg = config.get_llm_config().get(self._spec.name, {}) or {}
        self.api_key = _resolve_key(self._spec, cfg)
        self._credential_source = cfg.get("credential_source", "env")
        self.model_type = cfg.get("model") or get_default_model(self._spec.name)
        self.last_response = None

        model_config = get_model_config(self.model_type)
        if model_config:
            self.max_tokens = model_config.max_completion_tokens or 8192
        else:
            self.max_tokens = 8192
            self.logger.info(
                f"Model '{self.model_type}' not in registry — using defaults "
                f"(declare it under 'models:' in providers.yaml to list it)"
            )
        self.supports_vision = self._resolve_supports_vision()
        self.temperature = 0.7

    def _resolve_supports_vision(self, model_type: Optional[str] = None) -> bool:
        mc = get_model_config(model_type or self.model_type)
        if mc:
            return mc.capabilities.supports_vision
        return self._spec.supports_vision

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
        import anthropic
        try:
            kwargs = {}
            if self._spec.bearer_auth:
                # z.ai's Anthropic-compatible endpoint authenticates with
                # Authorization: Bearer, not x-api-key.
                kwargs["auth_token"] = self.api_key
            else:
                kwargs["api_key"] = self.api_key
            base_url = self._profile_base_url()
            if base_url:
                kwargs["base_url"] = base_url
            headers = self._spec.headers()
            if headers:
                kwargs["default_headers"] = headers
            self._client = anthropic.AsyncAnthropic(**kwargs)
            self.logger.debug(f"{self._spec.display_name} client setup completed")
        except Exception as e:
            raise ServiceError(f"Failed to set up {self._spec.display_name} client: {e}")
