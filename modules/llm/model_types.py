"""Model registry TYPES — provider enum, pricing/capability/config dataclasses.

Split out of ``modules/llm/model_registry.py`` (S4, 2026-08-29); ``model_registry``
re-exports every name so existing importers are unaffected. Pure data + tiny helpers:
no registry state, no I/O.
"""
from dataclasses import dataclass
from typing import Dict, Optional, List
from enum import Enum
import logging
import re

logger = logging.getLogger(__name__)


class ModelProvider(Enum):
    """Supported model providers"""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    DEEPSEEK = "deepseek"
    OPENROUTER = "openrouter"
    NVIDIA = "nvidia"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Canonical agent-facing provider strings (WS-2.3: ONE source of truth)
# ---------------------------------------------------------------------------
# The rest of the agent stack (Agent.provider_name, the streaming whitelist, the
# schema-generator registry, native-tools reconciliation) keys off PROVIDER
# STRINGS, not the ModelProvider enum. Those strings had drifted across ≥4
# hand-rolled maps — most painfully ``GOOGLE`` whose enum VALUE is ``"google"``
# but whose canonical agent-facing string is ``"gemini"`` (a mismatch that
# silently disabled Gemini streaming). This map is the single source of truth;
# every enum→string conversion MUST go through ``canonical_provider_name`` so a
# new provider is added in exactly one place and can never drift again.
PROVIDER_CANONICAL_NAMES: Dict[ModelProvider, str] = {
    ModelProvider.OPENAI: "openai",
    ModelProvider.ANTHROPIC: "anthropic",
    ModelProvider.GOOGLE: "gemini",  # NOT "google" — see note above
    ModelProvider.DEEPSEEK: "deepseek",
    ModelProvider.OPENROUTER: "openrouter",
    ModelProvider.NVIDIA: "nvidia",
    ModelProvider.CUSTOM: "custom",
}

#: Every canonical provider string the agent stack recognises.
CANONICAL_PROVIDER_NAMES = frozenset(PROVIDER_CANONICAL_NAMES.values())

#: Canonical providers that support response streaming (everything but custom).
STREAMING_PROVIDER_NAMES = CANONICAL_PROVIDER_NAMES - {"custom"}


def canonical_provider_name(provider: ModelProvider, default: str = "generic") -> str:
    """Map a ``ModelProvider`` enum to its canonical agent-facing string.

    This is the ONLY sanctioned enum→string conversion. ``GOOGLE`` → ``"gemini"``
    (not its enum value ``"google"``). Returns *default* for an unknown provider.
    """
    return PROVIDER_CANONICAL_NAMES.get(provider, default)


# Cache-READ price multipliers vs base input price. Reads are the discounted,
# high-volume slice: Anthropic/OpenAI/DeepSeek/OpenRouter ~0.1x, Gemini implicit ~0.25x
# (verified 2026-07-02: OpenAI GPT-5.x cached = 0.1x; Anthropic cache read = 0.1x;
# DeepSeek already 0.028 = 0.1x of 0.28). Only used to DERIVE cached_input_price when a
# model leaves it unset; inert on models that never report cache hits.
CACHE_READ_PRICE_MULTIPLIER = {
    ModelProvider.ANTHROPIC: 0.1,
    ModelProvider.OPENAI: 0.1,
    ModelProvider.DEEPSEEK: 0.1,
    ModelProvider.OPENROUTER: 0.1,
    ModelProvider.NVIDIA: 0.1,
    ModelProvider.GOOGLE: 0.25,
}
_DEFAULT_CACHE_READ_MULTIPLIER = 0.1

# Cache-WRITE (creation) price multipliers vs base input price. Only providers that
# charge a per-token surcharge to WRITE a cache entry appear here. Anthropic bills
# cache creation at 1.25x input (verified 2026-07-02, doc'd in anthropic_client). OpenAI
# implicit caching, DeepSeek, and Gemini implicit caching have NO per-token write charge,
# so they are absent → cache-creation tokens bill at plain input price (no surcharge).
CACHE_WRITE_PRICE_MULTIPLIER = {
    ModelProvider.ANTHROPIC: 1.25,
}


@dataclass
class ModelPricing:
    """Model pricing information per 1M tokens"""
    input_price: float  # Price per 1M input tokens
    cached_input_price: Optional[float] = None  # Price for cached input (reads)
    output_price: float = 0.0  # Price per 1M output tokens
    # G3 (telemetry audit 2026-07-04): price to WRITE a cache entry (Anthropic 1.25x).
    # None => no per-token write surcharge; cache-creation tokens bill at input_price.
    cache_write_price: Optional[float] = None
    batch_api_available: bool = False
    currency: str = "USD"


@dataclass
class ModelCapabilities:
    """Model capabilities and features"""
    supports_vision: bool = False  # Default to False - only explicitly vision-capable models should be True
    supports_function_calling: bool = True
    supports_streaming: bool = True
    supports_json_mode: bool = True
    supports_system_messages: bool = True
    supports_tools: bool = True
    supports_audio: bool = False
    supports_realtime: bool = False
    supports_search: bool = False
    supports_computer_use: bool = False
    supports_thinking: bool = False
    # UP-07: per-model reasoning budget/effort. None => provider/SDK default (current
    # behavior). thinking_budget_tokens -> Anthropic thinking.budget_tokens / DeepSeek
    # max_cot_tokens; reasoning_effort -> OpenAI ("minimal"|"low"|"medium"|"high"|"none").
    thinking_budget_tokens: Optional[int] = None
    reasoning_effort: Optional[str] = None


@dataclass
class ModelConfig:
    """Complete model configuration"""
    name: str
    provider: ModelProvider
    context_window: int
    max_completion_tokens: int
    pricing: ModelPricing
    capabilities: ModelCapabilities
    chars_per_token: float = 4.0
    aliases: List[str] = None
    deprecated: bool = False
    knowledge_cutoff: Optional[str] = None
    # FIX (Jan 2026): Typical completion for dynamic reservation
    # Most agent responses are 500-2000 tokens, not 64K
    typical_completion_tokens: int = 4000  # Conservative typical usage
    # P0.4: optional human-readable name override for UI surfaces (e.g. a model
    # picker). None => derive a readable name from `name` (see
    # `_derive_display_name`). Hand-author overrides for flagship models when
    # the derived name reads badly.
    display_name_override: Optional[str] = None

    def __post_init__(self):
        if self.aliases is None:
            self.aliases = []
        # Bill cache reads at the provider's discounted rate instead of $0. Preserves any
        # explicit cached_input_price (DeepSeek + a few OpenRouter models). Safe: the cached
        # slice is only charged when cached_tokens>0, i.e. when the provider reported a hit.
        if self.pricing is not None and self.pricing.cached_input_price is None:
            _mult = CACHE_READ_PRICE_MULTIPLIER.get(self.provider, _DEFAULT_CACHE_READ_MULTIPLIER)
            self.pricing.cached_input_price = round(self.pricing.input_price * _mult, 6)
        # G3: derive cache-WRITE price for providers that surcharge cache creation
        # (Anthropic 1.25x). Absent providers keep None → billed at plain input price.
        if self.pricing is not None and self.pricing.cache_write_price is None:
            _wmult = CACHE_WRITE_PRICE_MULTIPLIER.get(self.provider)
            if _wmult is not None:
                self.pricing.cache_write_price = round(self.pricing.input_price * _wmult, 6)

    @property
    def display_name(self) -> str:
        """Human-readable name for UI surfaces (model picker, etc.).

        Returns `display_name_override` if set, else a name derived from
        `name` (vendor prefix stripped, title-cased, trailing date suffixed).
        """
        if self.display_name_override:
            return self.display_name_override
        return _derive_display_name(self.name)

    @property
    def effective_completion_reserve(self) -> int:
        """Get effective completion token reservation.

        FIX (Jan 2026): Dynamic reservation based on typical usage.
        Reserves 2x typical for safety, capped at max.
        This prevents wasting 47% of context on unused completion space.

        Returns:
            Token count to reserve for completion
        """
        # Reserve 2x typical for safety buffer
        dynamic_reserve = self.typical_completion_tokens * 2
        # Cap at max_completion_tokens
        return min(dynamic_reserve, self.max_completion_tokens)

    @property
    def safe_input_tokens(self) -> int:
        """Get safe input token limit (leaving room for completion).

        FIX (Jan 2026): Now uses effective_completion_reserve instead of
        max_completion_tokens for much better context utilization.
        """
        return self.context_window - self.effective_completion_reserve

    @property
    def min_safe_tokens(self) -> int:
        """Get minimum safe token limit for this model"""
        if self.context_window >= 500000:  # 500k+ models
            return 64000  # 64k minimum
        elif self.context_window >= 100000:  # 100k+ models
            return 32000  # 32k minimum
        elif self.context_window >= 32000:   # 32k+ models
            return 16000  # 16k minimum
        elif self.context_window >= 16000:   # 16k+ models
            return 8000   # 8k minimum
        else:
            return 4000   # 4k minimum for smaller models


# Acronym-ish vendor/model tokens that should stay upper-cased rather than
# title-cased (e.g. "glm" -> "GLM", not "Glm"). Plain-word tokens (e.g.
# "kimi") fall through to `str.capitalize()` -> "Kimi", not "KIMI".
_DISPLAY_NAME_ACRONYMS = {"glm", "gpt"}


def _derive_display_name(name: str) -> str:
    """Derive a human-readable display name from a raw model id.

    Strips a leading ``vendor/`` prefix, renders a trailing 4-digit date
    segment (e.g. ``-0905``) as a parenthesized suffix, and title-cases the
    remaining hyphen/underscore-separated words (with acronym handling for a
    few well-known short tokens). Used as the fallback for
    ``ModelConfig.display_name`` when no ``display_name_override`` is set.
    """
    base = name.split("/", 1)[-1]  # drop vendor prefix, e.g. "moonshotai/"
    match = re.search(r"-(\d{4})$", base)  # trailing 4-digit date -> "(0905)"
    suffix = f" ({match.group(1)})" if match else ""
    if match:
        base = base[: match.start()]
    words = base.replace("_", "-").split("-")

    def _cap(word: str) -> str:
        if word.lower() in _DISPLAY_NAME_ACRONYMS or word.isdigit():
            return word.upper()
        return word.capitalize()

    return " ".join(_cap(word) for word in words if word) + suffix
