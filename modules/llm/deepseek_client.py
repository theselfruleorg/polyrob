"""DeepSeek LLM API Client — DeepSeek's own OpenAI-compatible API.

DeepSeek speaks OpenAI ``/chat/completions`` with a bearer token, so this client
subclasses :class:`OpenRouterClient` (like :class:`NvidiaClient`) and overrides only
what DeepSeek really differs in (verified against https://api-docs.deepseek.com/
and the live API on 2026-10-27):

* **Thinking mode is ON by default** and the answer carries ``reasoning_content``.
  On a request WITH ``tools`` the docs require every earlier assistant message to
  send its ``reasoning_content`` back (else 400). The adapter stamps it on the
  per-call ``AIMessage`` and replays it (``ROUND_TRIP_REASONING``); this client only
  keeps it on the outgoing assistant messages.
* **No OpenRouter extras** — no provider routing, ``usage.include`` or
  ``reasoning`` block. With ``THINKING_CONFIG_ENABLED`` on, the registry's
  ``reasoning_effort`` (low|high|max) is sent; otherwise DeepSeek's default
  (thinking on, effort high). The legacy id ``deepseek-chat`` is Flash with
  thinking OFF.
* **Cost** — DeepSeek bills a fixed published price, DOUBLE on weekdays
  01:00–04:00 and 06:00–10:00 UTC. The catalog holds the off-peak price; the
  client reports the exact charge as ``billed_cost_usd`` (cache hits included).
  Chinese public holidays are off-peak and are billed here at the peak rate,
  so the figure is an upper bound on those days only.

Docs: https://api-docs.deepseek.com/guides/thinking_mode
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from openai import AsyncOpenAI

from modules.llm.openrouter_client import OpenRouterClient
from modules.llm.model_registry import get_model_config
from core.exceptions import ServiceError
from core.config import BotConfig

#: Weekday UTC hours billed at the peak (2x) rate — api-docs.deepseek.com/quick_start/pricing
PEAK_HOURS_UTC = frozenset({1, 2, 3, 6, 7, 8, 9})
PEAK_MULTIPLIER = 2.0


#: Retired ids the API still serves (live 2026-10-27): ``deepseek-chat`` = Flash with
#: thinking OFF, ``deepseek-reasoner`` = Flash with thinking ON. Sent as typed.
API_LEGACY_IDS = frozenset({"deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash"})


def wire_model_id(name: Optional[str]) -> Optional[str]:
    """The id DeepSeek's API accepts for *name*. A catalog alias the API rejects
    (``deepseek-v4.1-flash`` → 400) is sent as its canonical row name; an unknown
    name is sent as typed (a model newer than the catalog)."""
    if not name or name in API_LEGACY_IDS:
        return name
    from modules.llm.model_registry import get_model_config_exact, ModelProvider
    cfg = get_model_config_exact(name)
    if cfg is not None and cfg.provider == ModelProvider.DEEPSEEK:
        return cfg.name
    return name


def is_peak(now: Optional[datetime] = None) -> bool:
    """True inside DeepSeek's weekday peak-price windows (UTC)."""
    now = now or datetime.now(timezone.utc)
    return now.weekday() < 5 and now.hour in PEAK_HOURS_UTC


class DeepSeekClient(OpenRouterClient):
    """DeepSeek client — the OpenAI-compatible pipeline of :class:`OpenRouterClient`
    with DeepSeek's credentials, thinking knob, reasoning replay and price clock."""

    DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
    _PROVIDER_LABEL = "DeepSeek"
    #: The adapter replays each assistant message's ``reasoning_content``.
    ROUND_TRIP_REASONING = True

    @property
    def model_type(self) -> Optional[str]:
        return self._model_type

    @model_type.setter
    def model_type(self, name: Optional[str]) -> None:
        # Every request, validation and usage record reads this: keep it an API id.
        self._model_type = wire_model_id(name)

    def __init__(self, config: BotConfig, name: str = "deepseek_client"):
        # Skip OpenRouterClient.__init__ (it reads the 'openrouter' block).
        from modules.llm.llm_client import LLMClient
        from modules.llm.llm_client_registry import get_default_model
        LLMClient.__init__(self, config=config, name=name)
        self._client = None

        deepseek_config = config.get_llm_config().get('deepseek', {})
        self.api_key = deepseek_config.get('api_key')
        self._api_url_override = deepseek_config.get('api_url')
        self.model_type = deepseek_config.get('model') or get_default_model('deepseek')
        self.site_url = ''
        self.site_name = ''
        self.last_response = None

        model_config = get_model_config(self.model_type)
        self.max_tokens = model_config.max_completion_tokens if model_config else 32768
        self.supports_vision = self._resolve_supports_vision()
        self.temperature = 0.7   # no effect in thinking mode (DeepSeek ignores it)

    def _validate_llm_config(self) -> None:
        if not self.api_key:
            raise ServiceError("DeepSeek API key not provided (set DEEPSEEK_API_KEY)")

    def _base_url(self) -> str:
        """``DEEPSEEK_API_URL`` wins, else the profile, else the constant."""
        return (self._api_url_override or self._resolve_profile_base_url("deepseek")
                or self.DEEPSEEK_BASE_URL)

    async def _setup_client(self) -> None:
        try:
            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self._base_url())
        except Exception as e:
            raise ServiceError(f"Failed to set up DeepSeek client: {e}")

    def _get_openrouter_headers(self) -> Dict[str, str]:
        return {}

    def request_extras(self, request_params: Dict[str, Any],
                       max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Replaces the OpenRouter extras (see ``openrouter_reasoning.apply_request_extras``)."""
        from modules.llm.prefix_stamp import stamp_client
        from modules.llm.model_registry import get_thinking_config, thinking_config_enabled
        stamp_client(self, request_params)
        if thinking_config_enabled():
            effort = get_thinking_config(self.model_type).get('reasoning_effort')
            if effort:
                request_params['reasoning_effort'] = effort
        return request_params

    def _extract_usage_data(self) -> Dict[str, Optional[int]]:
        """Shared usage + DeepSeek's cache-hit count and its exact charge."""
        out = super()._extract_usage_data()
        usage = getattr(self.last_response, 'usage', None)
        if usage is None or out.get('prompt_tokens') is None:
            return out
        if not out.get('cached_tokens'):
            out['cached_tokens'] = getattr(usage, 'prompt_cache_hit_tokens', 0) or 0
        if out.get('billed_cost_usd') is None:
            from modules.llm.model_pricing import calculate_cost
            cost = calculate_cost(self.model_type, out['prompt_tokens'],
                                  out.get('completion_tokens') or 0, out['cached_tokens'])
            out['billed_cost_usd'] = cost * (PEAK_MULTIPLIER if is_peak() else 1.0)
        return out
