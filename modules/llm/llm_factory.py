"""Native LLM chat-model factory.

Builds POLYROB's native chat-model adapters (``modules.llm.adapters``) for each
provider — ``create_chat_model`` returns a native ``BaseChatModel`` (POLYROB's own
ABC). The agent loop, tool-calling, prompt-caching and token-counting are all
native, with no third-party agent-framework dependency. Raises on an unknown
provider (no silent provider swap).

``PROVIDER_CONFIG`` (model_registry) says which providers exist, and the provider's spec
selects the adapter.
"""

import logging
from typing import Optional

# NATIVE base type (POLYROB's own ABC)
from modules.llm.adapters import BaseChatModel
from core.lazy_deps import FeatureUnavailable
from modules.llm.llm_client import LLMClient
from modules.llm.model_registry import get_model_config, PROVIDER_CONFIG
from modules.llm.model_types import openai_reasoning_model

logger = logging.getLogger(__name__)

_GEMINI_SHORTHAND = {
    "gemini-3": "gemini-3-pro-preview",
    "gemini-3-pro": "gemini-3-pro-preview",
    "gemini": "gemini-2.5-flash",
}


def _get_model_max_tokens_from_registry(model: str) -> Optional[int]:
    """Get appropriate max_tokens limit from model registry.

    This is the ONLY place that should determine max_tokens.
    All hardcoded fallbacks have been removed - trust the model registry.
    """
    if not model:
        return None

    max_completion = get_model_config(model).max_completion_tokens
    # The model's completion CEILING is not a per-request size: a flash model advertises
    # 384000, and a credit-metered provider reserves what a request asks for. Resolve the
    # SAME per-request cap every call is clamped to (``_adjust_max_tokens`` ->
    # ``output_token_cap``), so the value stored and logged here is one a request can carry.
    from modules.llm.llm_client import _DEFAULT_OUTPUT_TOKEN_CAP
    from modules.llm.output_budget import output_token_cap
    cap = output_token_cap(_DEFAULT_OUTPUT_TOKEN_CAP)
    logger.debug(f"max_tokens for {model}: model ceiling {max_completion}, per-request cap {cap}")
    return min(max_completion, cap)


def _sync_client_model(llm_client: LLMClient, model: str, *, capabilities: bool) -> None:
    """Keep a shared client's ``model_type`` (and its capabilities) in sync with the request."""
    if not hasattr(llm_client, 'model_type') or llm_client.model_type == model:
        return
    logger.info(f"Updating {type(llm_client).__name__} model_type: {llm_client.model_type} -> {model}")
    llm_client.model_type = model
    if capabilities:
        model_config = get_model_config(model)
        llm_client.supports_vision = model_config.capabilities.supports_vision
        llm_client.max_tokens = model_config.max_completion_tokens


def _openai_adapter(model: str, llm_client: LLMClient, params: dict) -> BaseChatModel:
    from modules.llm.adapters import OpenAIAdapter
    sanitized_params = {k: v for k, v in params.items() if k != "model"}
    # An o-series reasoning model rejects these.
    if openai_reasoning_model(model):
        for key in ("parallel_tool_calls", "temperature", "max_tokens"):
            sanitized_params.pop(key, None)
    logger.info(f"Creating OpenAIAdapter for model {model}")
    return OpenAIAdapter(client=llm_client, model_name=model, **sanitized_params)


def _anthropic_adapter(model: str, llm_client: LLMClient, params: dict) -> BaseChatModel:
    from core.lazy_deps import ensure_provider
    ensure_provider("provider.anthropic")  # no-op when present
    from modules.llm.adapters import AnthropicAdapter
    sanitized_params = {k: v for k, v in params.items() if k != "model"}
    logger.info(f"Creating AnthropicAdapter for model {model}")
    return AnthropicAdapter(client=llm_client, model_name=model, **sanitized_params)


def _gemini_adapter(model: str, llm_client: LLMClient, params: dict) -> BaseChatModel:
    from core.lazy_deps import ensure_provider
    ensure_provider("provider.gemini")  # no-op when present
    from modules.llm.adapters import GeminiAdapter
    # Strip "models/" prefix and map shorthand names to official codes
    clean_model = model.replace("models/", "") if model.startswith("models/") else model
    clean_model = _GEMINI_SHORTHAND.get(clean_model, clean_model)
    _sync_client_model(llm_client, clean_model, capabilities=True)
    sanitized_params = {k: v for k, v in params.items()
                        if k not in ("parallel_tool_calls", "tool_choice", "model")}
    logger.info(f"Creating GeminiAdapter for model {clean_model}")
    return GeminiAdapter(client=llm_client, model_name=clean_model, **sanitized_params)


def _openrouter_adapter(model: str, llm_client: LLMClient, params: dict, *,
                        sync_capabilities: bool) -> BaseChatModel:
    """Every client on the OpenAI-compatible or Responses wire: they share one adapter, and
    the wire translation lives in the client."""
    from modules.llm.adapters import OpenRouterAdapter
    from modules.llm.deepseek_client import DeepSeekClient
    _sync_client_model(llm_client, model, capabilities=sync_capabilities)
    sanitized_params = {k: v for k, v in params.items()
                        if k not in ("parallel_tool_calls", "model")}
    adapter = OpenRouterAdapter
    if isinstance(llm_client, DeepSeekClient):   # the one seat that sends reasoning back
        from modules.llm.adapters import DeepSeekAdapter
        adapter = DeepSeekAdapter
    logger.info(f"Creating {adapter.__name__} for {model}")
    return adapter(client=llm_client, model_name=model, **sanitized_params)


def create_chat_model(
    provider: str,
    model: str,
    temperature: float,
    llm_client: LLMClient,
    **kwargs
) -> BaseChatModel:
    """Return a NATIVE chat-model adapter for the requested provider.

    Args:
        provider: Provider name ('openai', 'anthropic', 'deepseek', 'gemini',
            'openrouter', 'nvidia')
        model: Model name to use
        temperature: Temperature for generation
        llm_client: Instance of LLMClient for the corresponding provider
        **kwargs: Additional parameters to pass to the adapter

    Returns:
        Native ``BaseChatModel`` adapter wrapping ``llm_client``.

    Raises:
        ValueError: If the provider is unsupported (no silent fallback to a
            different provider — that masks misconfiguration).
    """
    from modules.llm.billing_guard import require_billable_model
    require_billable_model(model, provider)
    # Intelligent max_tokens from model registry
    max_tokens = kwargs.pop('max_tokens', None)
    if max_tokens is None:
        max_tokens = _get_model_max_tokens_from_registry(model)

    # llm_client is for internal use only — never forward it into adapter kwargs
    filtered_kwargs = {k: v for k, v in kwargs.items() if k != 'llm_client'}

    common_params = {
        "temperature": temperature,
        "model": model,
        **filtered_kwargs,
    }
    if max_tokens is not None:
        common_params["max_tokens"] = max_tokens
        logger.info(f"Using max_tokens={max_tokens} for {provider}:{model}")
    else:
        logger.info(f"No max_tokens limit set for {provider}:{model} (model self-regulates)")

    provider_l = provider.lower()

    # Guard: reject unknown providers early via PROVIDER_CONFIG (single source of truth).
    # This preserves the "no silent provider swap" contract while the list of known
    # providers lives in exactly one place (model_registry.PROVIDER_CONFIG).
    if provider_l not in PROVIDER_CONFIG:
        # A declared provider whose TRANSPORT has no client is a different
        # failure from an unknown name — name it, so the user is not left
        # scanning a 30-entry "Supported:" list for a typo.
        try:
            from modules.llm.provider_spec import generic_client_class_name, get_spec
            spec = get_spec(provider_l)
        except Exception:
            spec = None
        if spec is not None and spec.client_class_name is None \
                and generic_client_class_name(spec.transport) is None:
            raise ValueError(
                f"provider '{provider}' speaks the '{spec.transport.value}' API, "
                "which POLYROB cannot serve yet — no client implements that wire "
                "format. Connecting and storing its credential works; inference "
                "does not."
            )
        raise ValueError(
            f"Unsupported LLM provider '{provider}'. Supported: "
            + ", ".join(sorted(PROVIDER_CONFIG.keys()))
            + "."
        )

    # Stamp the resolved cache strategy once so clients read self.cache_strategy
    # instead of re-deriving caching ad hoc. Default "none" if unset.
    try:
        from modules.llm.cache_hints import provider_cache_strategy
        llm_client.cache_strategy = provider_cache_strategy(provider_l, model)
    except Exception:
        pass

    # The spec selects the adapter: a bespoke client's own parameter rules, else its transport.
    try:
        from modules.llm.provider_spec import Transport, get_spec
        spec = get_spec(provider_l)
        client_class = spec.client_class_name if spec else None
        if client_class == "OpenAIClient":
            return _openai_adapter(model, llm_client, common_params)
        if client_class == "GeminiClient":
            return _gemini_adapter(model, llm_client, common_params)
        if spec is not None and spec.transport is Transport.ANTHROPIC_MESSAGES:
            return _anthropic_adapter(model, llm_client, common_params)
        if spec is not None and spec.transport in (Transport.CHAT_COMPLETIONS, Transport.RESPONSES):
            return _openrouter_adapter(model, llm_client, common_params,
                                       sync_capabilities=client_class is not None)
        raise ValueError(
            f"Unsupported LLM provider '{provider}'"
            + (f" (transport '{spec.transport.value}' not implemented)" if spec else "")
            + ". Supported: " + ", ".join(sorted(PROVIDER_CONFIG.keys())) + "."
        )

    except ValueError:
        raise
    except FeatureUnavailable:
        # An absent provider SDK carries its own remedy (the extra to
        # install / add to PROD_EXTRAS); wrapping it below would bury that in a
        # generic "Failed to create chat model" string.
        raise
    except Exception as e:
        logger.error(f"Error creating native chat model for provider {provider}: {str(e)}")
        raise ValueError(f"Failed to create chat model for provider '{provider}': {str(e)}")
