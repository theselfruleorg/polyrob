"""
LLM Module - Language Model Management

This module provides:
- Model registry with configurations, pricing, and capabilities
- Token counting and cost estimation
- LLM client management and factory
- Adapters for various providers
- Native message types

Lazy package (PEP 562): the provider clients and adapters import their SDKs
(openai/anthropic/google) at module load, so importing `modules.llm` (or any leaf such
as `modules.llm.profiles`) must NOT eager-import them. Every public name resolves on first
attribute access via __getattr__, keeping leaf imports SDK-free for the CLI and server boot.
See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (P0c).
"""

from typing import TYPE_CHECKING

# Public name -> relative module that defines it. Resolved lazily by __getattr__.
_LAZY: dict[str, str] = {}


def _register(module: str, *names: str) -> None:
    for name in names:
        _LAZY[name] = module


_register(".messages", "BaseMessage", "SystemMessage", "HumanMessage", "AIMessage",
          "ToolMessage", "ChatGeneration", "ChatResult")
_register(".model_registry", "ModelConfig", "ModelProvider", "ModelPricing",
          "ModelCapabilities", "get_model_config", "register_custom_model",
          "get_all_models", "calculate_cost")
_register(".token_counter", "TokenUsage", "count_tokens", "count_messages_tokens",
          "track_usage", "estimate_cost")
_register(".llm_manager", "LLMManager")
_register(".llm_client", "LLMClient")
_register(".openai_client", "OpenAIClient")
_register(".anthropic_client", "AnthropicClient")
_register(".gemini_client", "GeminiClient")
_register(".deepseek_client", "DeepSeekClient")
_register(".openrouter_client", "OpenRouterClient")
_register(".adapters", "BaseChatModel", "LLMClientAdapter", "OpenAIAdapter",
          "AnthropicAdapter", "DeepSeekAdapter", "GeminiAdapter", "OpenRouterAdapter",
          "DeepSeekAgentAdapter", "GeminiAgentAdapter")
_register(".llm_factory", "create_chat_model")
_register(".llm_client_registry", "create_llm_client", "AVAILABLE_MODELS",
          "DEFAULT_MODELS", "get_default_model")

# Modules historically guarded by try/except — absence is tolerated (raises AttributeError).
_OPTIONAL_MODULES = {".adapters", ".llm_factory", ".llm_client_registry"}

if TYPE_CHECKING:  # static analysis / IDEs only — no runtime import
    from .messages import (BaseMessage, SystemMessage, HumanMessage, AIMessage,
                           ToolMessage, ChatGeneration, ChatResult)
    from .model_registry import (ModelConfig, ModelProvider, ModelPricing,
                                 ModelCapabilities, get_model_config,
                                 register_custom_model, get_all_models, calculate_cost)
    from .token_counter import (TokenUsage, count_tokens, count_messages_tokens,
                                track_usage, estimate_cost)
    from .llm_manager import LLMManager
    from .llm_client import LLMClient
    from .openai_client import OpenAIClient
    from .anthropic_client import AnthropicClient
    from .gemini_client import GeminiClient
    from .deepseek_client import DeepSeekClient
    from .openrouter_client import OpenRouterClient
    from .adapters import (BaseChatModel, LLMClientAdapter, OpenAIAdapter,
                           AnthropicAdapter, DeepSeekAdapter, GeminiAdapter,
                           OpenRouterAdapter, DeepSeekAgentAdapter, GeminiAgentAdapter)
    from .llm_factory import create_chat_model
    from .llm_client_registry import (create_llm_client, AVAILABLE_MODELS,
                                      DEFAULT_MODELS, get_default_model)


def __getattr__(name: str):
    """PEP 562 lazy resolution; caches into globals() so it fires once per name."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    try:
        mod = importlib.import_module(module, __name__)
    except ImportError:
        if module in _OPTIONAL_MODULES:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        raise
    value = getattr(mod, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))


__all__ = list(_LAZY)
