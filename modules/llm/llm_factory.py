"""Native LLM chat-model factory.

Builds POLYROB's native chat-model adapters (``modules.llm.adapters``) for each
provider — ``create_chat_model`` returns a native ``BaseChatModel`` (POLYROB's own
ABC). The agent loop, tool-calling, prompt-caching and token-counting are all
native, with no third-party agent-framework dependency. Raises on an unknown
provider (no silent provider swap).

Provider→adapter dispatch is driven by ``PROVIDER_CONFIG`` (model_registry),
the single source of truth for which providers exist. Provider-specific
parameter sanitisation remains inline since it is not the duplicated concern.
"""

import logging
from typing import Optional

# NATIVE base type (POLYROB's own ABC)
from modules.llm.adapters import BaseChatModel
from modules.llm.llm_client import LLMClient
from modules.llm.model_registry import get_model_config, PROVIDER_CONFIG, openai_reasoning_model

# Centralized timeout/retry constants. LLMClient.DEFAULT_REQUEST_TIMEOUT (=120) is the
# canonical mirror of TimeoutConfig.LLM_REQUEST_TIMEOUT. We read it from LLMClient (L1)
# rather than agents.task.constants (L2): a capability lib must not import up into the
# agent layer at module load, which dragged the whole agents/__init__ fan-out (+aiogram)
# into anything importing modules.llm. Equality is guarded by tests.
# See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (P0-L).
DEFAULT_REQUEST_TIMEOUT = LLMClient.DEFAULT_REQUEST_TIMEOUT
DEFAULT_MAX_RETRIES = LLMClient.DEFAULT_MAX_RETRIES

logger = logging.getLogger(__name__)


def _get_model_max_tokens_from_registry(model: str, provider: str) -> Optional[int]:
    """Get appropriate max_tokens limit from model registry.

    This is the ONLY place that should determine max_tokens.
    All hardcoded fallbacks have been removed - trust the model registry.
    """
    if not model:
        return None

    config = get_model_config(model)
    if config:
        max_completion = config.max_completion_tokens
        if max_completion and max_completion > 0:
            logger.debug(f"Using max_tokens={max_completion} for {model} from model registry")
            return max_completion

    logger.warning(
        f"Model '{model}' not found in registry. "
        f"No max_tokens limit will be set (model will self-regulate). "
        f"Consider adding this model to model_registry.py"
    )
    return None


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
    # Intelligent max_tokens from model registry
    max_tokens = kwargs.pop('max_tokens', None)
    if max_tokens is None:
        max_tokens = _get_model_max_tokens_from_registry(model, provider)

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
        raise ValueError(
            f"Unsupported LLM provider '{provider}'. Supported: "
            + ", ".join(sorted(PROVIDER_CONFIG.keys()))
            + "."
        )

    # UP-08: stamp the resolved cache strategy once so clients read self.cache_strategy
    # instead of re-deriving caching ad hoc. Default "none" if unset (back-compatible).
    try:
        from modules.llm.cache_hints import provider_cache_strategy
        llm_client.cache_strategy = provider_cache_strategy(provider_l, model)
    except Exception:
        pass

    # Provider-specific adapter construction. The if/elif chain below handles
    # parameter sanitisation that differs per provider — NOT client-class
    # selection (that now comes from PROVIDER_CONFIG). The order of branches
    # is preserved for readability but the fallthrough ValueError is replaced
    # by the early guard above.
    try:
        if provider_l == "openai":
            from modules.llm.adapters import OpenAIAdapter
            from modules.llm.openai_client import OpenAIClient
            if not isinstance(llm_client, OpenAIClient):
                logger.warning(f"Expected OpenAIClient but got {type(llm_client).__name__}")

            sanitized_params = common_params.copy()
            # H1: o-series reasoning models (o1/o3/o4) reject these params. SSOT helper —
            # prefix-based, so o3/o4-mini are covered (the old substring list missed them).
            if openai_reasoning_model(model):
                for key in ["parallel_tool_calls", "temperature", "max_tokens"]:
                    sanitized_params.pop(key, None)
            sanitized_params.pop("model", None)

            logger.info(f"Creating OpenAIAdapter for model {model}")
            return OpenAIAdapter(client=llm_client, model_name=model, **sanitized_params)

        elif provider_l == "anthropic":
            from modules.llm.adapters import AnthropicAdapter
            from modules.llm.anthropic_client import AnthropicClient
            if not isinstance(llm_client, AnthropicClient):
                logger.warning(f"Expected AnthropicClient but got {type(llm_client).__name__}")

            sanitized_params = common_params.copy()
            sanitized_params.pop("model", None)

            logger.info(f"Creating AnthropicAdapter for model {model}")
            return AnthropicAdapter(client=llm_client, model_name=model, **sanitized_params)

        elif provider_l == "deepseek":
            from modules.llm.adapters import DeepSeekAdapter
            from modules.llm.deepseek_client import DeepSeekClient
            if not isinstance(llm_client, DeepSeekClient):
                logger.warning(f"Expected DeepSeekClient but got {type(llm_client).__name__}, trying to adapt anyway")

            sanitized_params = common_params.copy()
            for key in ("parallel_tool_calls", "tool_choice", "model"):
                sanitized_params.pop(key, None)

            logger.info(f"Creating DeepSeekAdapter for model {model}")
            return DeepSeekAdapter(client=llm_client, model_name=model, **sanitized_params)

        elif provider_l in ("openrouter", "nvidia"):
            # NVIDIA NIM is OpenAI-compatible exactly like OpenRouter, and NvidiaClient
            # subclasses OpenRouterClient — so it rides the same adapter path.
            from modules.llm.adapters import OpenRouterAdapter
            from modules.llm.openrouter_client import OpenRouterClient
            if not isinstance(llm_client, OpenRouterClient):
                logger.warning(f"Expected OpenRouterClient but got {type(llm_client).__name__}, trying to adapt anyway")

            # Keep shared client's model_type/capabilities in sync with the request
            if hasattr(llm_client, 'model_type') and llm_client.model_type != model:
                logger.info(f"Updating OpenRouterClient model_type: {llm_client.model_type} -> {model}")
                llm_client.model_type = model
                model_config = get_model_config(model)
                if model_config:
                    llm_client.supports_vision = model_config.capabilities.supports_vision
                    llm_client.max_tokens = model_config.max_completion_tokens or 32768

            sanitized_params = common_params.copy()
            for key in ("parallel_tool_calls", "model"):
                sanitized_params.pop(key, None)

            logger.info(f"Creating OpenRouterAdapter for model {model}")
            return OpenRouterAdapter(client=llm_client, model_name=model, **sanitized_params)

        elif provider_l == "gemini":
            from modules.llm.adapters import GeminiAdapter
            from modules.llm.gemini_client import GeminiClient
            if not isinstance(llm_client, GeminiClient):
                logger.warning(f"Expected GeminiClient but got {type(llm_client).__name__}, trying to adapt anyway")

            # Strip "models/" prefix and map shorthand names to official codes
            clean_model = model.replace("models/", "") if model.startswith("models/") else model
            GEMINI_SHORTHAND_MAPPING = {
                "gemini-3": "gemini-3-pro-preview",
                "gemini-3-pro": "gemini-3-pro-preview",
                "gemini": "gemini-2.5-flash",
            }
            if clean_model in GEMINI_SHORTHAND_MAPPING:
                clean_model = GEMINI_SHORTHAND_MAPPING[clean_model]

            if hasattr(llm_client, 'model_type') and llm_client.model_type != clean_model:
                logger.info(f"Updating GeminiClient model_type: {llm_client.model_type} -> {clean_model}")
                llm_client.model_type = clean_model
                model_config = get_model_config(clean_model)
                if model_config:
                    llm_client.supports_vision = model_config.capabilities.supports_vision
                    llm_client.max_tokens = model_config.max_completion_tokens or 8192

            sanitized_params = common_params.copy()
            for key in ("parallel_tool_calls", "tool_choice", "model"):
                sanitized_params.pop(key, None)

            logger.info(f"Creating GeminiAdapter for model {clean_model}")
            return GeminiAdapter(client=llm_client, model_name=clean_model, **sanitized_params)

        else:
            # Should be unreachable — the PROVIDER_CONFIG guard above catches all
            # unknown providers before we enter this branch. Belt-and-suspenders.
            raise ValueError(
                f"Unsupported LLM provider '{provider}'. Supported: "
                + ", ".join(sorted(PROVIDER_CONFIG.keys()))
                + "."
            )

    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Error creating native chat model for provider {provider}: {str(e)}")
        raise ValueError(f"Failed to create chat model for provider '{provider}': {str(e)}")
