r"""
Model Registry for LLM Service

This module centralizes all model configurations for currently available models.
Single source of truth for model information based on official docs.

Sources:
- OpenAI: https://platform.openai.com/docs/models
- Anthropic: https://docs.anthropic.com/en/docs/about-claude/models
- Google: https://ai.google.dev/gemini-api/docs/models
- DeepSeek: https://api-docs.deepseek.com/

Layout:
  * ``model_types``   — ``ModelProvider``, ``ModelPricing``, ``ModelCapabilities``,
                        ``ModelConfig``, cache multipliers, display-name helper
  * ``model_catalog`` — the built-in ``ModelConfig`` rows (data only)
  * ``model_pricing`` — ``calculate_cost``
  * this module       — ``ModelRegistry`` + lookup helpers + ``PROVIDER_CONFIG``, and
                        re-exports of every public name so existing importers keep working.
"""
from typing import Dict, Optional, List
import logging

from modules.llm.model_types import (  # noqa: F401  (re-exported public surface)
    CACHE_READ_PRICE_MULTIPLIER,
    CACHE_WRITE_PRICE_MULTIPLIER,
    CANONICAL_PROVIDER_NAMES,
    ModelCapabilities,
    ModelConfig,
    ModelPricing,
    ModelProvider,
    PROVIDER_CANONICAL_NAMES,
    _DEFAULT_CACHE_READ_MULTIPLIER,
    _DISPLAY_NAME_ACRONYMS,
    _derive_display_name,
    _family_target,
    canonical_provider_name,
)
from modules.llm.model_pricing import calculate_cost  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Central registry for all model configurations"""

    def __init__(self):
        self._models: Dict[str, ModelConfig] = {}
        self._aliases: Dict[str, str] = {}  # alias -> canonical name
        self._warned_unknown: set = set()
        self._initialize_models()

    def _initialize_models(self):
        """Register the built-in catalog (``modules/llm/model_catalog.py``)."""
        from modules.llm.model_catalog import register_builtin_models
        register_builtin_models(self._register_model)

    def _register_model(self, config: ModelConfig):
        """Register a model and its aliases"""
        # Register main name
        self._models[config.name] = config

        # Register aliases
        for alias in config.aliases:
            self._aliases[alias] = config.name

        logger.debug(f"Registered model: {config.name} with {len(config.aliases)} aliases")

    def get_model(self, name: str) -> ModelConfig:
        """The row a capability read uses, never None: the exact row or alias, else
        the row of the same family, else a conservative default that carries the spec's
        vision. Billing reads ``get_model_config_exact`` instead."""
        exact = self._models.get(self._aliases.get(name, name))
        if exact:
            return exact
        # Warn once per unknown id, not once per token count.
        if name not in self._warned_unknown:
            self._warned_unknown.add(name)
            logger.warning(f"Model '{name}' has no catalog row; using its family or a "
                           f"conservative default")
        target = _family_target(str(name or "").lower())
        return self._models.get(target) if target in self._models else _conservative_config(name)

    def list_models(self, provider: Optional[ModelProvider] = None,
                   include_deprecated: bool = False) -> List[ModelConfig]:
        """List all registered models, optionally filtered by provider"""
        models = list(self._models.values())

        if provider:
            models = [m for m in models if m.provider == provider]

        if not include_deprecated:
            models = [m for m in models if not m.deprecated]

        return models

    def get_model_names(self, provider: Optional[ModelProvider] = None,
                       include_deprecated: bool = False) -> List[str]:
        """Get list of model names"""
        models = self.list_models(provider, include_deprecated)
        return [m.name for m in models]


#: The limits of a model with no row and no family. Small enough that the context budget
#: fires early, and a request never asks for more output than the model may give.
UNKNOWN_MODEL_CONTEXT_WINDOW = 128000
UNKNOWN_MODEL_MAX_COMPLETION = 8192


def _spec_declares_vision(name: str) -> bool:
    """True when each provider spec that declares *name* says it sees images. A spec
    that does not declare *name* says nothing, so the answer is the safe one."""
    from modules.llm.provider_spec import BUILTIN_SPECS, get_specs
    try:
        specs = get_specs()
    except Exception:
        specs = BUILTIN_SPECS  # a broken providers.yaml must not break a capability read
    declaring = [s for s in specs if name in s.models or name == s.default_model]
    return bool(declaring) and all(s.supports_vision for s in declaring)


def _conservative_config(name: str) -> ModelConfig:
    """A row for an id the catalog does not know. It is not registered, so a price read
    through ``get_model_config_exact`` stays None. Such an id speaks chat completions and
    ``detect_llm_provider`` keys the tool schema on the provider: a ``vendor/model`` id is an
    OpenRouter slug, so a build with no provider names OpenRouter, and a bare id names OpenAI."""
    return ModelConfig(
        name=str(name or ""),
        provider=ModelProvider.OPENROUTER if "/" in str(name or "") else ModelProvider.OPENAI,
        context_window=UNKNOWN_MODEL_CONTEXT_WINDOW,
        max_completion_tokens=UNKNOWN_MODEL_MAX_COMPLETION,
        pricing=ModelPricing(input_price=0.0, output_price=0.0),
        capabilities=ModelCapabilities(supports_vision=_spec_declares_vision(name)),
    )


# Singleton instance
_registry = None

def get_registry() -> ModelRegistry:
    """Get the singleton model registry"""
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry


def get_model_config(model_name: str) -> ModelConfig:
    """The row a capability read uses, never None (``ModelRegistry.get_model``)."""
    return get_registry().get_model(model_name)


def thinking_config_enabled() -> bool:
    """Gate for SENDING per-model thinking params (Anthropic thinking block,
    DeepSeek / OpenAI reasoning_effort).

    Default **OFF** so the hot path is byte-identical today — enabling extended thinking
    is a real behavior change (Anthropic forces temperature=1 + streaming, adds reasoning
    cost). The budgets live in the registry as the single source of truth regardless;
    this flag controls whether they're applied. Enable with THINKING_CONFIG_ENABLED in
    {1, true, yes, on}.
    """
    from core.env import bool_env as _bool_env
    return _bool_env("THINKING_CONFIG_ENABLED", False)


def get_thinking_config(model_name: str) -> Dict[str, object]:
    """Per-model reasoning config. Empty dict => no thinking params (provider/SDK
    default, current behavior). Returns {"budget_tokens": int} and/or
    {"reasoning_effort": str} for a thinking-capable model that has them set.
    """
    config = get_model_config(model_name)
    if not config.capabilities.supports_thinking:
        return {}
    out: Dict[str, object] = {}
    budget = config.capabilities.thinking_budget_tokens
    effort = config.capabilities.reasoning_effort
    if budget:
        out["budget_tokens"] = budget
    if effort:
        out["reasoning_effort"] = effort
    return out


def register_custom_model(config: ModelConfig):
    """Register a custom model configuration

    Args:
        config: ModelConfig object with model details
    """
    registry = get_registry()
    registry._register_model(config)
    logger.info(f"Registered custom model: {config.name}")


def get_all_models(provider: Optional[ModelProvider] = None,
                   include_deprecated: bool = False) -> List[ModelConfig]:
    """Get all registered models

    Args:
        provider: Optional filter by provider
        include_deprecated: Include deprecated models

    Returns:
        List of ModelConfig objects
    """
    registry = get_registry()
    return registry.list_models(provider, include_deprecated)


def get_model_config_exact(model_name: str):
    """Model config by exact name or declared alias — NO family fallback.

    ``get_model_config`` falls back by family and ultimately to a conservative
    default, which is right for capability questions and WRONG for billing: it has
    no price for an id it does not know. Billing uses this instead.
    """
    registry = get_registry()
    canonical = registry._aliases.get(model_name, model_name)
    return registry._models.get(canonical)


def provider_supports_streaming(name: str) -> bool:
    """True when the provider's client streams: every spec row, built-in or user-declared,
    except the RESPONSES transport, whose client has no stream path."""
    from modules.llm.provider_spec import BUILTIN_SPECS, Transport, get_spec
    name = (name or "").lower()
    try:
        spec = get_spec(name)
    except Exception:
        spec = next((s for s in BUILTIN_SPECS if name == s.name or name in s.aliases), None)
    return spec is not None and spec.transport is not Transport.RESPONSES


# ---------------------------------------------------------------------------
# PROVIDER_CONFIG — single source of truth for provider→client mapping
# ---------------------------------------------------------------------------
# Maps the canonical provider string (same strings used throughout the agent
# stack) to a lightweight record describing which client class to instantiate
# and whether the provider participates in the automatic fallback hierarchy.
#
# Rules:
#   fallback_eligible=True  → provider is in LLMManager.FALLBACK_HIERARCHY
#   fallback_eligible=False → provider is NOT in the fallback hierarchy;
#                             can still be constructed explicitly
#
# Client classes are imported lazily inside the record factory to avoid
# circular imports at module load time.
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True)
class _ProviderEntry:
    """Metadata for one LLM provider."""
    # String used throughout the agent stack — matches PROVIDER_CANONICAL_NAMES
    provider: str
    # Uninstantiated client class (lazy import avoids circular deps at load)
    client_class_name: str   # e.g. "AnthropicClient" — for documentation
    # Whether this provider participates in LLMManager.FALLBACK_HIERARCHY.
    fallback_eligible: bool


def _build_provider_config() -> "Dict[str, _ProviderEntry]":
    """Build PROVIDER_CONFIG dict.

    The entries are DERIVED from the ProviderSpec
    registry (built-ins + user-declared providers.yaml rows; a user row's
    client class is the generic client for its declared transport). A registry
    fault falls back to ``BUILTIN_SPECS`` — the registry's own data — never to a
    second hand-maintained table.
    """
    from modules.llm.provider_spec import BUILTIN_SPECS, generic_client_class_name, get_specs
    try:
        specs = get_specs()
    except Exception:
        specs = BUILTIN_SPECS  # provider lookup must never crash on a bad providers.yaml
    out: "Dict[str, _ProviderEntry]" = {}
    for s in specs:
        cls = s.client_class_name or generic_client_class_name(s.transport)
        if cls is None:
            continue  # transport with no generic client yet (RESPONSES)
        out[s.name] = _ProviderEntry(s.name, cls, fallback_eligible=s.fallback_eligible)
    return out


class _LazyProviderConfig:
    """Proxy that builds PROVIDER_CONFIG on first access to avoid circular imports."""

    def __init__(self) -> None:
        self._config: "Optional[Dict[str, _ProviderEntry]]" = None

    def _ensure(self) -> "Dict[str, _ProviderEntry]":
        if self._config is None:
            self._config = _build_provider_config()
        return self._config

    # dict-like interface
    def __getitem__(self, key: str) -> _ProviderEntry:
        return self._ensure()[key]

    def __contains__(self, key: object) -> bool:
        return key in self._ensure()

    def keys(self):
        return self._ensure().keys()

    def values(self):
        return self._ensure().values()

    def items(self):
        return self._ensure().items()

    def get(self, key: str, default=None):
        return self._ensure().get(key, default)


#: Single source of truth for provider → client-class + fallback eligibility.
#: Use PROVIDER_CONFIG[provider_str] throughout the LLM subsystem instead of
#: maintaining per-file client maps.
PROVIDER_CONFIG = _LazyProviderConfig()