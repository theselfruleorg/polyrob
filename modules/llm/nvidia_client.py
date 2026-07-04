"""NVIDIA NIM LLM API Client.

NVIDIA's hosted inference endpoint (https://integrate.api.nvidia.com/v1) is fully
OpenAI-compatible and offers a free tier — handy for endless testing of models like
``moonshotai/kimi-k2.6``.

Because the wire format is identical to OpenRouter (OpenAI ``/chat/completions``
with a bearer token), this client subclasses :class:`OpenRouterClient` and only
overrides what differs: where it reads credentials, the transport base URL, and
the (absent) app-attribution headers. All message formatting, tool-calling, token
accounting and error translation are inherited unchanged.

Docs: https://docs.api.nvidia.com/nim/reference/llm-apis
"""

import logging
from typing import Dict

from openai import AsyncOpenAI

from modules.llm.openrouter_client import OpenRouterClient
from modules.llm.model_registry import get_model_config
from core.exceptions import ServiceError
from core.config import BotConfig


class NvidiaClient(OpenRouterClient):
    """NVIDIA NIM client — OpenAI-compatible access to Kimi, Llama, and more.

    Inherits the full request/response/tool-calling pipeline from
    :class:`OpenRouterClient`; only credentials, base URL and headers differ.
    """

    # Fallback base URL; the declarative source is the "nvidia" ProviderProfile (P8).
    NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

    # Name used in inherited log/exception messages (overrides OpenRouterClient).
    _PROVIDER_LABEL = "NVIDIA"

    def _profile_base_url(self) -> str:
        """Transport base URL sourced from the declarative ProviderProfile (P8),
        falling back to the hardcoded constant if the profile is unavailable.

        Delegates to the base ``_resolve_profile_base_url`` helper; keeps the
        class-level constant as the fallback.
        """
        return self._resolve_profile_base_url("nvidia") or self.NVIDIA_BASE_URL

    def _resolve_supports_vision(self, model_type=None) -> bool:
        """Override: NVIDIA's un-registered fallback is False (not True).

        When the model IS in the registry the registry value is authoritative
        (a registered NVIDIA model with vision=True should still report True).
        Only the "not in registry" branch differs from the base.
        """
        from modules.llm.model_registry import get_model_config as _gmc
        name = model_type if model_type is not None else self.model_type
        try:
            mc = _gmc(name)
            if mc is not None:
                return mc.capabilities.supports_vision
        except Exception:
            pass
        return False  # NVIDIA: assume NO vision if model not registered

    def __init__(self, config: BotConfig, name: str = "nvidia_client"):
        """Initialize the NVIDIA NIM client."""
        # Intentionally skip OpenRouterClient.__init__ (it reads the 'openrouter'
        # config block); call the grandparent LLMClient initializer directly.
        from modules.llm.llm_client import LLMClient
        LLMClient.__init__(self, config=config, name=name)
        self._client = None

        nvidia_config = config.get_llm_config().get('nvidia', {})
        self.api_key = nvidia_config.get('api_key')
        # Allow an explicit endpoint override (NVIDIA_API_URL) to win over the profile.
        self._api_url_override = nvidia_config.get('api_url')
        self.model_type = nvidia_config.get('model', 'moonshotai/kimi-k2.6')

        # OpenRouter attribution headers don't apply to NVIDIA.
        self.site_url = ''
        self.site_name = ''

        self.last_response = None

        model_config = get_model_config(self.model_type)
        if model_config:
            self.max_tokens = model_config.max_completion_tokens
        else:
            self.max_tokens = 16384
            self.logger.warning(f"Model '{self.model_type}' not found in registry, using defaults")

        # Resolve vision support via overridden helper (NVIDIA fallback = False)
        self.supports_vision = self._resolve_supports_vision()

        self.temperature = 0.7

        self.logger.debug(
            f"NVIDIA client initialized: model={self.model_type}, "
            f"max_tokens={self.max_tokens}, supports_vision={self.supports_vision}"
        )

    def _validate_llm_config(self) -> None:
        """Validate LLM config."""
        if not self.api_key:
            raise ServiceError("NVIDIA API key not provided (set NVIDIA_API_KEY)")

    def _base_url(self) -> str:
        """Effective base URL: explicit NVIDIA_API_URL override wins, else profile."""
        return self._api_url_override or self._profile_base_url()

    async def _setup_client(self) -> None:
        """Set up the NVIDIA client using the OpenAI SDK."""
        try:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self._base_url(),
            )
            self.logger.debug("NVIDIA client setup completed")
        except Exception as e:
            raise ServiceError(f"Failed to set up NVIDIA client: {e}")

    def _get_openrouter_headers(self) -> Dict[str, str]:
        """NVIDIA needs no app-attribution headers."""
        return {}
