"""Built-in model CATALOG — the data half of the model registry (current as of Dec 2025).

Split out of ``modules/llm/model_registry.py`` (S4, 2026-08-29). This module holds ONLY
``ModelConfig`` rows; ``ModelRegistry._initialize_models`` calls ``register_builtin_models``
with its ``_register_model`` bound method. Pricing math lives in ``model_pricing``; the
enum/dataclasses in ``model_types``.
"""
from typing import Callable

from modules.llm.model_types import ModelCapabilities, ModelConfig, ModelPricing, ModelProvider


def register_builtin_models(register: Callable[[ModelConfig], None]) -> None:
    """Register every built-in model through *register* (one ``ModelConfig`` per call)."""

    # ========================================
    # OPENAI MODELS
    # ========================================

    # GPT-5.1 Series (Released Nov 12, 2025)
    # Source: https://openai.com/index/gpt-5-1-for-developers/
    # Features: Adaptive thinking, apply_patch tool, shell tool
    register(ModelConfig(
        name="gpt-5.1",
        provider=ModelProvider.OPENAI,
        context_window=400000,  # 400K tokens
        max_completion_tokens=128000,  # 128K output
        pricing=ModelPricing(input_price=2.00, output_price=8.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_thinking=True  # Adaptive reasoning (can disable with reasoning_effort='none')
        ),
        knowledge_cutoff="2025-11",
        aliases=["gpt-5.1-chat-latest", "gpt-5-1", "gpt-5.1-thinking"]
    ))

    register(ModelConfig(
        name="gpt-5.1-mini",
        provider=ModelProvider.OPENAI,
        context_window=400000,  # 400K tokens
        max_completion_tokens=128000,  # 128K output
        pricing=ModelPricing(input_price=0.40, output_price=1.60),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_thinking=True
        ),
        knowledge_cutoff="2025-11",
        aliases=["gpt-5.1-instant", "gpt-5-1-mini"]
    ))

    # GPT-5.1 Codex Series (Specialized for coding)
    register(ModelConfig(
        name="gpt-5.1-codex",
        provider=ModelProvider.OPENAI,
        context_window=400000,
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=3.00, output_price=12.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_thinking=True
        ),
        knowledge_cutoff="2025-11",
        aliases=["gpt-5-1-codex"]
    ))

    register(ModelConfig(
        name="gpt-5.1-codex-mini",
        provider=ModelProvider.OPENAI,
        context_window=400000,
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=0.60, output_price=2.40),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_thinking=True
        ),
        knowledge_cutoff="2025-11",
        aliases=["gpt-5-1-codex-mini"]
    ))

    register(ModelConfig(
        name="gpt-5.1-codex-max",
        provider=ModelProvider.OPENAI,
        context_window=400000,
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=10.00, output_price=40.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_thinking=True
        ),
        knowledge_cutoff="2025-11",
        aliases=["gpt-5-1-codex-max"]
    ))

    # GPT-5 Series (Released Aug 7, 2025)
    register(ModelConfig(
        name="gpt-5",
        provider=ModelProvider.OPENAI,
        context_window=400000,  # 400K tokens
        max_completion_tokens=128000,  # 128K output
        pricing=ModelPricing(input_price=1.25, output_price=10.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_thinking=True
        ),
        knowledge_cutoff="2025-08",
        aliases=["gpt-5-preview", "gpt-5-2025"]
    ))

    register(ModelConfig(
        name="gpt-5-pro",
        provider=ModelProvider.OPENAI,
        context_window=400000,
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=15.00, output_price=60.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_thinking=True
        ),
        knowledge_cutoff="2025-08"
    ))

    register(ModelConfig(
        name="gpt-5-mini",
        provider=ModelProvider.OPENAI,
        context_window=400000,
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=0.25, output_price=2.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True
        ),
        knowledge_cutoff="2025-08"
    ))

    register(ModelConfig(
        name="gpt-5-nano",
        provider=ModelProvider.OPENAI,
        context_window=400000,
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=0.05, output_price=0.40),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True
        ),
        knowledge_cutoff="2025-08"
    ))

    # GPT-4.1 Series (Released Apr 2025) - 1M context!
    # Source: https://openai.com/index/gpt-4-1/
    register(ModelConfig(
        name="gpt-4.1",
        provider=ModelProvider.OPENAI,
        context_window=1000000,  # 1M tokens!
        max_completion_tokens=32768,  # 32K output
        pricing=ModelPricing(input_price=2.00, output_price=8.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True
        ),
        knowledge_cutoff="2025-04",
        aliases=["gpt-4.1-2025-04-14", "gpt-4-1"]
    ))

    register(ModelConfig(
        name="gpt-4.1-mini",
        provider=ModelProvider.OPENAI,
        context_window=1000000,  # 1M tokens
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.40, output_price=1.60),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True
        ),
        knowledge_cutoff="2025-04",
        aliases=["gpt-4.1-mini-2025-04-14", "gpt-4-1-mini"]
    ))

    register(ModelConfig(
        name="gpt-4.1-nano",
        provider=ModelProvider.OPENAI,
        context_window=1000000,  # 1M tokens
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.10, output_price=0.40),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True
        ),
        knowledge_cutoff="2025-04",
        aliases=["gpt-4.1-nano-2025-04-14", "gpt-4-1-nano"]
    ))

    # Reasoning Models (o-series)
    # NOTE: O-series models are reasoning-focused and do NOT support vision
    register(ModelConfig(
        name="o3",
        provider=ModelProvider.OPENAI,
        context_window=262144,
        max_completion_tokens=65536,
        pricing=ModelPricing(input_price=10.00, output_price=40.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,
            supports_vision=False,  # O-series doesn't support vision
            supports_function_calling=False  # O-series doesn't support tools
        ),
        knowledge_cutoff="2025-10",
        aliases=["o3-2025"]
    ))

    register(ModelConfig(
        name="o4-mini",
        provider=ModelProvider.OPENAI,
        context_window=262144,
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=1.00, output_price=4.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,
            supports_vision=False,  # O-series doesn't support vision
            supports_function_calling=False  # O-series doesn't support tools
        ),
        knowledge_cutoff="2025-10"
    ))

    # GPT-4o - DEPRECATED (Oct 2025)
    register(ModelConfig(
        name="gpt-4o",
        provider=ModelProvider.OPENAI,
        context_window=128000,
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=2.50, output_price=10.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_audio=True
        ),
        knowledge_cutoff="2024-10",
        aliases=["gpt-4o-2024-11-20", "gpt-4o-latest"],
        deprecated=True
    ))

    # ========================================
    # ANTHROPIC MODELS
    # Source: https://docs.anthropic.com/en/docs/about-claude/models
    # DEPRECATED ALIASES: Claude 3.x names are mapped onto the 4.5 models below
    # for backward compatibility.
    # ========================================

    # ----------------------------------------
    # CURRENT LINEUP (Fable 5 / Opus 4.6-4.8 / Sonnet 4.6-5)
    # All 1M-context natively (no beta header — cross-checked vs the live
    # OpenRouter anthropic mirror, 2026-07-14) and 128K max output. Extended
    # thinking is ADAPTIVE on these models — `budget_tokens` is rejected (400)
    # on Fable 5 / Opus 4.7-4.8 / Sonnet 5 and deprecated on Opus 4.6 /
    # Sonnet 4.6 — so `thinking_budget_tokens` is left unset (adaptive/effort
    # is the control, get_thinking_config() returns {} => no thinking params
    # sent, byte-identical to today). max_completion_tokens kept at the 64000
    # family convention (128K output would require streaming). Pricing from
    # the claude-api model catalog (cached 2026-06-24).
    # ----------------------------------------

    # Claude Fable 5 - most capable widely-released model (thinking always on)
    register(ModelConfig(
        name="claude-fable-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=10.00, output_price=50.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,  # always-on adaptive; budget_tokens rejected
        ),
        chars_per_token=5.0,
        aliases=["claude-fable", "fable-5", "fable", "claude-fable5"]
    ))

    # Claude Opus 4.8 - most capable Opus-tier (current flagship Opus)
    register(ModelConfig(
        name="claude-opus-4-8",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=5.00, output_price=25.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,  # adaptive; budget_tokens rejected (400)
        ),
        chars_per_token=5.0,
        aliases=["claude-opus-4.8", "claude-4.8-opus", "opus-4.8", "opus-4-8"]
    ))

    # Claude Opus 4.7 - previous-generation Opus
    register(ModelConfig(
        name="claude-opus-4-7",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=5.00, output_price=25.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,  # adaptive; budget_tokens rejected (400)
        ),
        chars_per_token=5.0,
        aliases=["claude-opus-4.7", "opus-4.7", "opus-4-7"]
    ))

    # Claude Opus 4.6 - older Opus (adaptive thinking recommended)
    register(ModelConfig(
        name="claude-opus-4-6",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=5.00, output_price=25.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,  # adaptive (budget_tokens deprecated here)
        ),
        chars_per_token=5.0,
        aliases=["claude-opus-4.6", "opus-4.6", "opus-4-6"]
    ))

    # Claude Sonnet 5 - near-Opus quality on coding/agentic at Sonnet cost.
    # Standard pricing $3/$15 (intro $2/$10 through 2026-08-31); the standard
    # rate is used here so cost estimates never under-bill during the intro.
    register(ModelConfig(
        name="claude-sonnet-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=3.00, output_price=15.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,  # adaptive on by default; budget_tokens rejected
        ),
        chars_per_token=5.0,
        aliases=["claude-sonnet5", "claude-5-sonnet", "sonnet-5"]
    ))

    # Claude Sonnet 4.6 - previous-generation Sonnet
    register(ModelConfig(
        name="claude-sonnet-4-6",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=3.00, output_price=15.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,  # adaptive (budget_tokens deprecated here)
        ),
        chars_per_token=5.0,
        aliases=["claude-sonnet-4.6", "sonnet-4.6", "sonnet-4-6"]
    ))

    # ----------------------------------------
    # LEGACY LINEUP (still active; default anthropic model = claude-sonnet-4-5)
    # ----------------------------------------

    # Claude 4.5 Series
    register(ModelConfig(
        name="claude-sonnet-4-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=200000,  # 1M with beta header
        max_completion_tokens=64000,  # FIXED: Anthropic max is 64000, not 65536
        pricing=ModelPricing(input_price=3.00, output_price=15.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,  # Extended thinking supported
            thinking_budget_tokens=64000,  # UP-07 (was AnthropicClient.EXTENDED_THINKING_MODELS)
        ),
        chars_per_token=5.0,
        knowledge_cutoff="2025-01",
        aliases=[
            # Current aliases
            "claude-sonnet-4.5", "claude-4.5-sonnet", "claude-sonnet-4", "claude-sonnet-4-5-20250929",
            # DEPRECATED Claude 3.5 Sonnet aliases (maps to 4.5 for backward compat)
            "claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20240620",
            "claude-3-5-sonnet-latest", "claude-3-5-sonnet", "claude-3.5-sonnet",
            "claude-3-sonnet-20240229", "claude-3-sonnet"
        ]
    ))

    register(ModelConfig(
        name="claude-haiku-4-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=200000,
        max_completion_tokens=64000,  # FIXED: Anthropic max is 64000, not 65536
        pricing=ModelPricing(input_price=1.00, output_price=5.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,  # Extended thinking supported
            thinking_budget_tokens=64000,  # UP-07 (was AnthropicClient.EXTENDED_THINKING_MODELS)
        ),
        chars_per_token=5.0,
        knowledge_cutoff="2025-02",
        aliases=[
            # Current aliases
            "claude-haiku-4.5", "claude-4.5-haiku", "claude-haiku-4-5-20251001",
            # DEPRECATED Claude 3.5 Haiku aliases (maps to 4.5 for backward compat)
            "claude-3-5-haiku-20241022", "claude-3-5-haiku-latest", "claude-3-5-haiku",
            "claude-3.5-haiku", "claude-3-haiku-20240307", "claude-3-haiku"
        ]
    ))

    # Claude Opus 4.5 - NEW Premium model (Nov 2025)
    # Maximum intelligence with practical performance
    register(ModelConfig(
        name="claude-opus-4-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=200000,
        max_completion_tokens=64000,  # FIXED: Anthropic max is 64000, not 65536
        pricing=ModelPricing(input_price=5.00, output_price=25.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,  # Extended thinking supported
            thinking_budget_tokens=64000,  # UP-07 (was AnthropicClient.EXTENDED_THINKING_MODELS)
        ),
        chars_per_token=5.0,
        knowledge_cutoff="2025-03",
        aliases=[
            # Current aliases
            "claude-opus-4.5", "claude-4.5-opus", "claude-opus-4-5-20251101",
            # DEPRECATED Claude 3 Opus aliases (maps to 4.5 for backward compat)
            "claude-3-opus-20240229", "claude-3-opus", "claude-3.0-opus"
        ]
    ))

    # Claude Opus 4.1 - Specialized reasoning (legacy)
    register(ModelConfig(
        name="claude-opus-4-1",
        provider=ModelProvider.ANTHROPIC,
        context_window=200000,
        max_completion_tokens=32768,  # 32K output
        pricing=ModelPricing(input_price=15.00, output_price=75.00),
        capabilities=ModelCapabilities(
            supports_thinking=True,
            thinking_budget_tokens=32768,  # UP-07 (was AnthropicClient.EXTENDED_THINKING_MODELS)
        ),
        chars_per_token=5.0,
        knowledge_cutoff="2025-01",
        aliases=["claude-4-opus", "claude-opus-4", "claude-opus-4.1", "claude-opus-4-1-20250805"]
    ))

    # Claude 3.x Series - REMOVED (deprecated Oct 2025)
    # Deprecated model names are now aliases to Claude 4.5 models above

    # ========================================
    # GOOGLE GEMINI MODELS (December 2025)
    # Source: https://ai.google.dev/gemini-api/docs/models
    # Official model codes from Google API documentation
    # ========================================

    # ----------------------------------------
    # GEMINI 3 SERIES - PREVIEW (Nov 2025)
    # ----------------------------------------

    # Gemini 3 Pro Preview - Best multimodal & agentic model
    # API Code: gemini-3-pro-preview
    # Supports: Text, Image, Video, Audio, PDF input -> Text output
    register(ModelConfig(
        name="gemini-3-pro-preview",
        provider=ModelProvider.GOOGLE,
        context_window=1048576,  # 1M tokens
        max_completion_tokens=65536,  # 65K output
        pricing=ModelPricing(input_price=2.50, output_price=10.00),
        capabilities=ModelCapabilities(
            supports_vision=True,  # Multimodal: Image, Video, Audio, PDF
            supports_thinking=True,  # Thinking supported
            supports_function_calling=True,  # Function calling supported
            supports_streaming=True,
            supports_json_mode=True,  # Structured outputs
            supports_tools=True
        ),
        knowledge_cutoff="2025-01",  # Per docs: January 2025
        aliases=["gemini-3-pro", "gemini-3", "gemini-3-preview"]
    ))

    # Gemini 3 Pro Image Preview - Image generation model
    # API Code: gemini-3-pro-image-preview
    # Supports: Image and Text input -> Image and Text output
    # NOTE: Does NOT support function calling!
    register(ModelConfig(
        name="gemini-3-pro-image-preview",
        provider=ModelProvider.GOOGLE,
        context_window=65536,  # 65K input
        max_completion_tokens=32768,  # 32K output
        pricing=ModelPricing(input_price=2.50, output_price=10.00),
        capabilities=ModelCapabilities(
            supports_vision=True,  # Image input
            supports_thinking=True,  # Thinking supported
            supports_function_calling=False,  # NOT supported per docs!
            supports_streaming=True,
            supports_json_mode=True,  # Structured outputs
            supports_tools=False  # NOT supported
        ),
        knowledge_cutoff="2025-01",
        aliases=["gemini-3-pro-image", "gemini-3-image"]
    ))

    # ----------------------------------------
    # GEMINI 2.5 SERIES - STABLE (June-Sept 2025)
    # ----------------------------------------

    # Gemini 2.5 Flash - Best price-performance model (STABLE)
    # API Code: gemini-2.5-flash
    # Fast, intelligent, large-scale processing
    register(ModelConfig(
        name="gemini-2.5-flash",
        provider=ModelProvider.GOOGLE,
        context_window=1048576,  # 1M tokens
        max_completion_tokens=65536,  # 65K output
        pricing=ModelPricing(input_price=0.075, output_price=0.30),  # Flash pricing
        capabilities=ModelCapabilities(
            supports_vision=True,  # Text, images, video, audio
            supports_thinking=True,  # Thinking supported
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_tools=True
        ),
        knowledge_cutoff="2025-01",
        aliases=["gemini-2.5-flash-stable", "gemini-flash-latest"]
    ))

    # Gemini 2.5 Flash Preview
    # API Code: gemini-2.5-flash-preview-09-2025
    register(ModelConfig(
        name="gemini-2.5-flash-preview-09-2025",
        provider=ModelProvider.GOOGLE,
        context_window=1048576,  # 1M tokens
        max_completion_tokens=65536,  # 65K output
        pricing=ModelPricing(input_price=0.075, output_price=0.30),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_tools=True
        ),
        knowledge_cutoff="2025-01",
        aliases=["gemini-2.5-flash-preview"]
    ))

    # ----------------------------------------
    # GEMINI 2.0 SERIES - STABLE (Feb 2025)
    # ----------------------------------------

    # Gemini 2.0 Flash - Fast multimodal model
    # API Code: gemini-2.0-flash
    register(ModelConfig(
        name="gemini-2.0-flash",
        provider=ModelProvider.GOOGLE,
        context_window=1048576,  # 1M tokens
        max_completion_tokens=8192,  # 8K output
        pricing=ModelPricing(input_price=0.10, output_price=0.40),
        capabilities=ModelCapabilities(
            supports_vision=True,  # Audio, images, video, text
            supports_thinking=False,  # Thinking is "Experimental" per docs
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_tools=True,
            supports_realtime=True  # Live API supported
        ),
        knowledge_cutoff="2024-08",
        aliases=["gemini-2.0-flash-001", "gemini-2.0-flash-exp"]
    ))

    # Gemini 2.0 Flash-Lite - Cost-efficient model
    # API Code: gemini-2.0-flash-lite
    register(ModelConfig(
        name="gemini-2.0-flash-lite",
        provider=ModelProvider.GOOGLE,
        context_window=1048576,  # 1M tokens
        max_completion_tokens=8192,  # 8K output
        pricing=ModelPricing(input_price=0.075, output_price=0.30),
        capabilities=ModelCapabilities(
            supports_vision=True,  # Audio, images, video, text
            supports_thinking=False,  # NOT supported per docs
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True,  # Structured outputs
            supports_tools=False  # NOT supported per docs
        ),
        knowledge_cutoff="2024-08",
        aliases=["gemini-2.0-flash-lite-001"]
    ))

    # ----------------------------------------
    # LEGACY - Gemini 2.5 Pro (for backwards compat)
    # ----------------------------------------
    # Note: Gemini 2.5 Pro is NOT in current docs, may be deprecated
    # Keeping for backwards compatibility with existing sessions
    register(ModelConfig(
        name="gemini-2.5-pro",
        provider=ModelProvider.GOOGLE,
        context_window=1048576,  # 1M tokens
        max_completion_tokens=65536,  # 65K output
        pricing=ModelPricing(input_price=1.25, output_price=5.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_tools=True
        ),
        knowledge_cutoff="2025-01",
        aliases=["gemini-2.5-pro-exp", "gemini-2.5-pro-preview-03-25", "gemini-pro"]
    ))

    # ========================================
    # DEEPSEEK MODELS (December 2025)
    # Source: https://api-docs.deepseek.com/quick_start/pricing
    # ========================================

    # DeepSeek-V3.2 Chat - Non-thinking mode (Released Dec 1, 2025)
    # 671B total params, 37B active per token (MoE architecture)
    register(ModelConfig(
        name="deepseek-chat",
        provider=ModelProvider.DEEPSEEK,
        context_window=128000,  # 128K context with DSA (DeepSeek Sparse Attention)
        max_completion_tokens=8192,  # 4K default, 8K max
        pricing=ModelPricing(
            input_price=0.28,  # $0.28/M tokens (cache miss)
            cached_input_price=0.028,  # $0.028/M tokens (cache hit)
            output_price=0.42  # $0.42/M tokens
        ),
        capabilities=ModelCapabilities(
            supports_function_calling=True,
            supports_tools=True,
            supports_json_mode=True,
            supports_vision=False,
            supports_streaming=True
        ),
        knowledge_cutoff="2025-12",
        aliases=["deepseek-v3.2", "deepseek-v3", "deepseek-chat-v3.2"]
    ))

    # DeepSeek-V3.2 Reasoner - Thinking mode (Released Dec 1, 2025)
    # Same architecture as chat but with extended thinking
    register(ModelConfig(
        name="deepseek-reasoner",
        provider=ModelProvider.DEEPSEEK,
        context_window=128000,  # 128K context
        max_completion_tokens=64000,  # 32K default, 64K max
        pricing=ModelPricing(
            input_price=0.28,  # $0.28/M tokens (cache miss)
            cached_input_price=0.028,  # $0.028/M tokens (cache hit)
            output_price=0.42  # $0.42/M tokens
        ),
        capabilities=ModelCapabilities(
            supports_thinking=True,
            thinking_budget_tokens=32000,  # UP-07: was deepseek_client DEFAULT_MAX_COT_TOKENS
            supports_function_calling=True,  # V3.2 reasoner now supports tools
            supports_tools=True,
            supports_json_mode=True,
            supports_vision=False,
            supports_streaming=True
        ),
        knowledge_cutoff="2025-12",
        aliases=["deepseek-v3.2-reasoner", "deepseek-r1", "deepseek-thinking"]
    ))

    # DeepSeek-V3.2-Speciale - Extended reasoning (Temporary until Dec 15, 2025)
    # Enhanced thinking mode with 128K output
    register(ModelConfig(
        name="deepseek-speciale",
        provider=ModelProvider.DEEPSEEK,
        context_window=128000,  # 128K context
        # Not in the models.dev oracle; aligned with the deepseek-reasoner
        # sibling (64K out / 128K ctx). reserve==window clamped the input
        # budget to the 1,000-token floor (2026-08-15 audit P6).
        max_completion_tokens=64000,
        pricing=ModelPricing(
            input_price=0.28,
            cached_input_price=0.028,
            output_price=0.42
        ),
        capabilities=ModelCapabilities(
            supports_thinking=True,
            supports_function_calling=False,  # Thinking mode only
            supports_tools=False,
            supports_vision=False  # DeepSeek doesn't support vision
        ),
        knowledge_cutoff="2025-12",
        aliases=["deepseek-v3.2-speciale"]
    ))

    # ========================================
    # OPENROUTER MODELS (December 2025)
    # Access via https://openrouter.ai/models
    # Pricing: $ per million tokens
    # ========================================

    # ----------------------------------------
    # Z.AI GLM MODELS (Zhipu AI via OpenRouter)
    # https://openrouter.ai/provider/z-ai
    # ----------------------------------------

    # GLM-5.2 - current Z.AI flagship on OpenRouter (default for `--provider
    # openrouter`). 1M context, native tools + reasoning + structured outputs,
    # text-only. OpenAI-compatible via OpenRouter (no client change needed).
    # Specs/pricing verified against the live OpenRouter models API (2026-06-18).
    register(ModelConfig(
        name="z-ai/glm-5.2",
        provider=ModelProvider.OPENROUTER,
        context_window=1048576,  # 1.05M tokens (OpenRouter API is authoritative)
        max_completion_tokens=32768,  # OpenRouter top_provider cap (re-verified 2026-07-14; was 262144)
        pricing=ModelPricing(input_price=0.93, cached_input_price=0.18, output_price=3.00),  # OpenRouter models API re-verified 2026-07-14 (was 1.20/4.10)
        capabilities=ModelCapabilities(
            supports_vision=False,  # text->text
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,  # response_format + structured_outputs
            supports_thinking=True,  # reasoning param ("reasoning"/include_reasoning)
            thinking_budget_tokens=32000,  # consumed only when THINKING_CONFIG_ENABLED
        ),
        knowledge_cutoff="2026-04",
        aliases=["glm", "glm-5.2", "glm5.2", "z-ai/glm"]
    ))

    # GLM-5 - balanced Gen-5 GLM (203K ctx). Cheaper/faster alternative to the
    # 5.2 flagship; same native tools + reasoning + structured outputs, text-only.
    register(ModelConfig(
        name="z-ai/glm-5",
        provider=ModelProvider.OPENROUTER,
        context_window=202752,  # 203K tokens
        max_completion_tokens=131072,  # family cap (OpenRouter API authoritative)
        pricing=ModelPricing(input_price=0.60, output_price=1.92),
        capabilities=ModelCapabilities(
            supports_vision=False,
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True,
            thinking_budget_tokens=32000,
        ),
        knowledge_cutoff="2026-04",
        aliases=["glm-5", "glm5"]
    ))

    # GLM-4.7 - cheapest proven GLM (203K ctx). Lowest latency/cost of the GLM
    # family registered here; native tools + reasoning + structured outputs, text-only.
    register(ModelConfig(
        name="z-ai/glm-4.7",
        provider=ModelProvider.OPENROUTER,
        context_window=202752,  # 203K tokens
        max_completion_tokens=131072,  # 128K max output
        pricing=ModelPricing(input_price=0.40, output_price=1.75),
        capabilities=ModelCapabilities(
            supports_vision=False,
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True,
            thinking_budget_tokens=32000,
        ),
        knowledge_cutoff="2026-01",
        aliases=["glm-4.7", "glm4.7"]
    ))

    # GLM-5.1 - Gen-5 GLM between 5 and 5.2 (203K ctx). Native tools +
    # reasoning + structured outputs, text-only. Verified 2026-07-14.
    register(ModelConfig(
        name="z-ai/glm-5.1",
        provider=ModelProvider.OPENROUTER,
        context_window=202752,  # 203K tokens
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=0.966, cached_input_price=0.1794, output_price=3.036),
        capabilities=ModelCapabilities(
            supports_vision=False,
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True,
            thinking_budget_tokens=32000,
        ),
        knowledge_cutoff="2026-04",
        aliases=["glm-5.1", "glm5.1"]
    ))

    # GLM-5-Turbo - faster/cheaper Gen-5 GLM (262K ctx). Native tools +
    # reasoning + structured outputs, text-only. Verified 2026-07-14.
    register(ModelConfig(
        name="z-ai/glm-5-turbo",
        provider=ModelProvider.OPENROUTER,
        context_window=262144,  # 262K tokens
        max_completion_tokens=131072,
        pricing=ModelPricing(input_price=1.20, cached_input_price=0.24, output_price=4.00),
        capabilities=ModelCapabilities(
            supports_vision=False,
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True,
            thinking_budget_tokens=32000,
        ),
        knowledge_cutoff="2026-03",
        aliases=["glm-5-turbo", "glm5-turbo"]
    ))

    # GLM-4.7-Flash - cheapest GLM tier (203K ctx), speed-optimized, text-only.
    # Native tools; reasoning off on the flash tier. Verified 2026-07-14.
    register(ModelConfig(
        name="z-ai/glm-4.7-flash",
        provider=ModelProvider.OPENROUTER,
        context_window=202752,  # 203K tokens
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=0.06, cached_input_price=0.01, output_price=0.40),
        capabilities=ModelCapabilities(
            supports_vision=False,
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
        ),
        knowledge_cutoff="2026-01",
        aliases=["glm-4.7-flash", "glm4.7-flash", "glm-flash"]
    ))

    # ----------------------------------------
    # GROK MODELS (xAI via OpenRouter)
    # https://openrouter.ai/provider/xai
    # ----------------------------------------

    # Grok 4.3 - current xAI flagship on OpenRouter (default for `--provider
    # openrouter`). xAI deprecated grok-4.1-fast (404s with a "switch to Grok
    # 4.3" message), so 4.3 is the preferred entry. Context/pricing mirror the
    # recent grok family; the OpenRouter API enforces the authoritative limits.
    register(ModelConfig(
        name="x-ai/grok-4.3",
        provider=ModelProvider.OPENROUTER,
        context_window=1000000,  # 1M tokens (OpenRouter re-verified 2026-07-14; was 2M)
        max_completion_tokens=30000,
        pricing=ModelPricing(input_price=1.25, cached_input_price=0.20, output_price=2.50),  # OpenRouter models API verified 2026-06-20, cache read re-verified 2026-07-14
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True
        ),
        knowledge_cutoff="2025-11",
        aliases=["grok-4.3", "grok-43", "grok"]
    ))

    # Grok 4.5 - newest xAI flagship on OpenRouter (created most-recently in the
    # live models API, 2026-07-14). 500K ctx, multimodal, native tools + reasoning.
    # Specs/pricing verified vs GET https://openrouter.ai/api/v1/models (2026-07-14).
    register(ModelConfig(
        name="x-ai/grok-4.5",
        provider=ModelProvider.OPENROUTER,
        context_window=500000,  # 500K tokens (OpenRouter API authoritative)
        max_completion_tokens=30000,
        pricing=ModelPricing(input_price=2.00, cached_input_price=0.50, output_price=6.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True,
        ),
        knowledge_cutoff="2026-04",
        aliases=["grok-4.5", "grok-45"]
    ))

    # Grok 4.20 - 2M-context multi-agent Grok; cheaper than 4.5. Native tools +
    # reasoning, multimodal. (x-ai/grok-4.20-multi-agent shares these specs.)
    register(ModelConfig(
        name="x-ai/grok-4.20",
        provider=ModelProvider.OPENROUTER,
        context_window=2000000,  # 2M tokens (OpenRouter API authoritative)
        max_completion_tokens=30000,
        pricing=ModelPricing(input_price=1.25, cached_input_price=0.20, output_price=2.50),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True,
        ),
        knowledge_cutoff="2026-03",
        aliases=["grok-4.20", "grok-420", "grok-4-20"]
    ))

    # Grok 4.1 Fast - DEPRECATED by xAI (kept for back-compat / historical ids;
    # OpenRouter now 404s this model and recommends Grok 4.3 above).
    # Created: Nov 19, 2025 | 2M context, web search, reasoning toggle
    register(ModelConfig(
        name="x-ai/grok-4.1-fast",
        provider=ModelProvider.OPENROUTER,
        context_window=2000000,  # 2M tokens
        max_completion_tokens=30000,  # Verified from OpenRouter docs
        pricing=ModelPricing(input_price=0.20, output_price=0.50),  # $0.40/$1.00 above 128k
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True  # Reasoning can be toggled
        ),
        knowledge_cutoff="2025-11",
        aliases=["grok-4.1-fast", "grok-4-1-fast", "grok-41-fast"]
    ))

    # Grok 4 Fast - Multimodal with 2M context
    # Created: Sep 19, 2025 | Non-reasoning and reasoning flavors
    register(ModelConfig(
        name="x-ai/grok-4-fast",
        provider=ModelProvider.OPENROUTER,
        context_window=2000000,  # 2M tokens
        max_completion_tokens=30000,  # Verified from OpenRouter docs
        pricing=ModelPricing(input_price=0.20, output_price=0.50),  # $0.40/$1.00 above 128k
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True  # Reasoning can be toggled
        ),
        knowledge_cutoff="2025-09",
        aliases=["grok-4-fast", "grok4-fast"]
    ))

    # Grok 4 - Reasoning model (mandatory reasoning)
    # Created: Jul 9, 2025 | 256K context, reasoning always on
    register(ModelConfig(
        name="x-ai/grok-4",
        provider=ModelProvider.OPENROUTER,
        context_window=256000,  # 256K tokens
        max_completion_tokens=32768,  # Not specified, conservative estimate
        pricing=ModelPricing(input_price=3.00, output_price=15.00),  # $6/$30 above 128k
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_function_calling=True,
            supports_thinking=True,  # Always on, cannot be disabled
            supports_streaming=True,
            supports_json_mode=True
        ),
        knowledge_cutoff="2025-07",
        aliases=["grok-4", "grok4"]
    ))

    # Grok Code Fast 1 - Agentic coding specialist
    # Created: Aug 26, 2025 | 256K context, visible reasoning traces
    register(ModelConfig(
        name="x-ai/grok-code-fast-1",
        provider=ModelProvider.OPENROUTER,
        context_window=256000,  # 256K tokens - verified
        max_completion_tokens=10000,  # Verified from OpenRouter docs
        pricing=ModelPricing(input_price=0.20, output_price=1.50),
        capabilities=ModelCapabilities(
            supports_function_calling=True,
            supports_thinking=True,  # Visible reasoning traces
            supports_streaming=True,
            supports_json_mode=True
        ),
        knowledge_cutoff="2025-08",
        aliases=["grok-code-fast", "grok-code-1", "grok-code"]
    ))

    # Grok 3 - Enterprise model with deep domain knowledge
    # Created: Jun 10, 2025 | Finance, healthcare, law, science
    register(ModelConfig(
        name="x-ai/grok-3",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,  # 131K tokens - verified
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=3.00, output_price=15.00),  # Higher above 128k
        capabilities=ModelCapabilities(
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True
        ),
        knowledge_cutoff="2025-06",
        aliases=["grok-3", "grok3"]
    ))

    # Grok 3 Mini - Lightweight thinking model
    # Created: Jun 10, 2025 | Fast, logic-based tasks
    register(ModelConfig(
        name="x-ai/grok-3-mini",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,  # 131K tokens - verified
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.30, output_price=0.50),
        capabilities=ModelCapabilities(
            supports_function_calling=True,
            supports_thinking=True,  # Thinks before responding
            supports_streaming=True
        ),
        knowledge_cutoff="2025-06",
        aliases=["grok-3-mini", "grok3-mini"]
    ))

    # ----------------------------------------
    # KIMI MODELS (Moonshot AI via OpenRouter)
    # 1T params MoE, 32B active per forward pass
    # NOTE: Kimi K2 models are TEXT-ONLY, no vision support
    # ----------------------------------------

    # Kimi K2 0905 - September 2025 update
    # 262K context (extended from 128K), improved agentic coding
    register(ModelConfig(
        name="moonshotai/kimi-k2-0905",
        provider=ModelProvider.OPENROUTER,
        context_window=262144,  # 262K tokens - verified (extended)
        max_completion_tokens=100352,  # models.dev openrouter limit.output (2026-08-15)
        pricing=ModelPricing(input_price=0.39, output_price=1.90),
        capabilities=ModelCapabilities(
            supports_vision=False,  # TEXT-ONLY - no image support
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True
        ),
        knowledge_cutoff="2025-09",
        aliases=["kimi-k2-0905", "kimi-k2", "kimi"]
    ))

    # Kimi K2 0711 - Original July release
    # 128K context, strong coding benchmarks
    register(ModelConfig(
        name="moonshotai/kimi-k2",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,  # 131K tokens - verified
        max_completion_tokens=100352,  # models.dev openrouter limit.output (2026-08-15)
        pricing=ModelPricing(input_price=0.456, output_price=1.84),
        capabilities=ModelCapabilities(
            supports_vision=False,  # TEXT-ONLY - no image support
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True
        ),
        knowledge_cutoff="2025-07",
        aliases=["kimi-k2-0711"]
    ))

    # Kimi K2 Thinking - Advanced reasoning model
    # Created: Nov 6, 2025 | 200-300 tool calls stability
    register(ModelConfig(
        name="moonshotai/kimi-k2-thinking",
        provider=ModelProvider.OPENROUTER,
        context_window=262144,  # 262K tokens - verified
        max_completion_tokens=16384,  # Verified from OpenRouter docs
        pricing=ModelPricing(input_price=0.45, output_price=2.35),
        capabilities=ModelCapabilities(
            supports_vision=False,  # TEXT-ONLY - no image support
            supports_function_calling=True,
            supports_thinking=True,  # Step-by-step reasoning
            supports_streaming=True
        ),
        knowledge_cutoff="2025-11",
        aliases=["kimi-k2-thinking", "kimi-thinking"]
    ))

    # ========================================
    # NVIDIA NIM MODELS (OpenAI-compatible, free hosted inference)
    # Access via https://integrate.api.nvidia.com/v1
    # build.nvidia.com offers a free tier — useful for endless testing.
    # ========================================

    # Kimi K2.6 - Moonshot's flagship served on NVIDIA NIM (free tier).
    # TEXT-ONLY (no vision); strong agentic/tool-calling. The provider id is
    # what NVIDIA expects on the wire: "moonshotai/kimi-k2.6".
    register(ModelConfig(
        name="moonshotai/kimi-k2.6",
        provider=ModelProvider.NVIDIA,
        context_window=262144,  # 262K tokens (Kimi K2 family)
        max_completion_tokens=16384,  # NVIDIA NIM default cap for this model
        pricing=ModelPricing(input_price=0.0, output_price=0.0),  # free hosted tier
        capabilities=ModelCapabilities(
            supports_vision=False,  # TEXT-ONLY - no image support
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True
        ),
        knowledge_cutoff="2025-11",
        aliases=["kimi-k2.6", "kimi-k26", "kimi-2.6"]
    ))

    # ----------------------------------------
    # QWEN MODELS (Alibaba via OpenRouter)
    # https://openrouter.ai/qwen
    # NOTE: Most Qwen3 models are TEXT-ONLY. Only Qwen3-VL models support vision.
    # ----------------------------------------

    # Qwen3 235B A22B - Flagship MoE model
    # 235B total, 22B active per forward pass
    register(ModelConfig(
        name="qwen/qwen3-235b-a22b",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,  # 131K with YaRN scaling - native 40K
        max_completion_tokens=40960,  # Verified from OpenRouter docs
        pricing=ModelPricing(input_price=0.455, output_price=1.82),  # OpenRouter models API verified 2026-06-20 (was 0.18/0.54 — ~3.4x too low)
        capabilities=ModelCapabilities(
            supports_vision=False,  # TEXT-ONLY - no image support
            supports_function_calling=True,
            supports_thinking=True,  # Dual mode: thinking/non-thinking
            supports_streaming=True,
            supports_json_mode=True
        ),
        knowledge_cutoff="2025-04",
        aliases=["qwen3-235b-a22b", "qwen3-235b", "qwen-235b", "qwen3"]
    ))

    # Qwen3 Max - Updated flagship (Sep 2025)
    # Major improvements in reasoning, 100+ languages
    register(ModelConfig(
        name="qwen/qwen3-max",
        provider=ModelProvider.OPENROUTER,
        context_window=256000,  # 256K tokens - verified
        max_completion_tokens=32768,  # Verified from OpenRouter docs
        pricing=ModelPricing(input_price=0.78, output_price=3.90),  # OpenRouter models API verified 2026-06-20 (was 1.20/6.00)
        capabilities=ModelCapabilities(
            supports_vision=False,  # TEXT-ONLY - no image support
            supports_function_calling=True,
            supports_streaming=True,
            supports_json_mode=True
        ),
        knowledge_cutoff="2025-09",
        aliases=["qwen3-max", "qwen-max"]
    ))

    # Qwen3 Coder 480B A35B - Code generation specialist
    # Created: Jul 23, 2025 | 480B total, 35B active (8/160 experts)
    register(ModelConfig(
        name="qwen/qwen3-coder",
        provider=ModelProvider.OPENROUTER,
        context_window=262144,  # 262K tokens - verified
        max_completion_tokens=65536,  # models.dev openrouter limit.output (2026-08-15)
        pricing=ModelPricing(input_price=0.22, output_price=0.95),  # Higher above 128k
        capabilities=ModelCapabilities(
            supports_vision=False,  # TEXT-ONLY - no image support
            supports_function_calling=True,
            supports_streaming=True
        ),
        knowledge_cutoff="2025-07",
        aliases=["qwen3-coder-480b", "qwen-coder", "qwen3-coder", "qwen3-coder-480b-a35b"]
    ))

    # Qwen3 32B - Dense model with thinking mode
    # Created: Apr 28, 2025 | 32.8B params, 100+ languages
    # NOTE: This is a TEXT-ONLY model, NOT vision capable
    register(ModelConfig(
        name="qwen/qwen3-32b",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,  # 131K with YaRN - native 40K
        max_completion_tokens=40960,  # Verified from OpenRouter docs
        pricing=ModelPricing(input_price=0.08, output_price=0.24),
        capabilities=ModelCapabilities(
            supports_vision=False,  # TEXT-ONLY - no image support
            supports_function_calling=True,
            supports_thinking=True,  # Dual mode
            supports_streaming=True
        ),
        knowledge_cutoff="2025-04",
        aliases=["qwen3-32b", "qwen-32b"]
    ))

    # Qwen3 VL 235B A22B Instruct - Vision-Language model
    # Created: Sep 23, 2025 | VQA, document parsing, video understanding
    register(ModelConfig(
        name="qwen/qwen3-vl-235b-a22b-instruct",
        provider=ModelProvider.OPENROUTER,
        context_window=262144,  # 262K tokens - verified
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.20, output_price=1.20),
        capabilities=ModelCapabilities(
            supports_vision=True,  # Images and video
            supports_function_calling=True,
            supports_streaming=True
        ),
        knowledge_cutoff="2025-09",
        aliases=["qwen3-vl-235b", "qwen-vl", "qwen3-vl"]
    ))

    # ----------------------------------------
    # DEEPSEEK MODELS (via OpenRouter)
    # https://openrouter.ai/deepseek
    # DeepSeek V3 with OpenRouter's tool calling support
    # ----------------------------------------

    # DeepSeek Chat V3 via OpenRouter - Tool calling supported
    # 671B total params, 37B active (MoE architecture)
    register(ModelConfig(
        name="deepseek/deepseek-chat",
        provider=ModelProvider.OPENROUTER,
        context_window=128000,  # 128K context
        max_completion_tokens=8192,  # 8K output
        pricing=ModelPricing(
            input_price=0.14,  # OpenRouter pricing
            output_price=0.28
        ),
        capabilities=ModelCapabilities(
            supports_vision=False,  # Text only
            supports_function_calling=True,  # Tool calling via OpenRouter
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True
        ),
        knowledge_cutoff="2025-12",
        aliases=["openrouter-deepseek", "openrouter-deepseek-chat", "deepseek-openrouter", "or-deepseek"]
    ))

    # DeepSeek R1 Reasoning via OpenRouter
    # Extended thinking with tool support
    register(ModelConfig(
        name="deepseek/deepseek-r1",
        provider=ModelProvider.OPENROUTER,
        context_window=128000,  # 128K context
        max_completion_tokens=64000,  # 64K output for reasoning
        pricing=ModelPricing(
            input_price=0.55,  # Higher for reasoning
            output_price=2.19
        ),
        capabilities=ModelCapabilities(
            supports_vision=False,  # Text only
            supports_function_calling=True,  # Tool calling via OpenRouter
            supports_tools=True,
            supports_thinking=True,  # Reasoning model
            supports_streaming=True
        ),
        knowledge_cutoff="2025-12",
        aliases=["openrouter-deepseek-r1", "deepseek-r1-openrouter", "or-deepseek-r1"]
    ))

    # DeepSeek R1 Distill Qwen 32B - Smaller reasoning model
    # Distilled from R1 into Qwen 32B architecture
    register(ModelConfig(
        name="deepseek/deepseek-r1-distill-qwen-32b",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,  # 131K context
        max_completion_tokens=16384,  # 16K output
        pricing=ModelPricing(
            input_price=0.12,  # Cheaper distilled model
            output_price=0.18
        ),
        capabilities=ModelCapabilities(
            supports_vision=False,
            supports_function_calling=True,
            supports_tools=True,
            supports_thinking=True,
            supports_streaming=True
        ),
        knowledge_cutoff="2025-01",
        aliases=["deepseek-r1-distill-qwen", "deepseek-r1-qwen-32b", "or-deepseek-r1-distill"]
    ))

    # ========================================
    # CURATED OPENROUTER SHORTLIST EXPANSION (2026-06-24)
    # Models ROB has no native client for (OpenRouter's real value). Pricing +
    # specs verified against the live OpenRouter models API on 2026-06-24
    # (GET https://openrouter.ai/api/v1/models). Pinned in
    # tests/unit/modules/llm/test_openrouter_pricing_verified_2026_06_24.py.
    # ========================================

    # --- Frontier agentic / coding ---

    # Moonshot Kimi K2.5 — strong agentic; ROB otherwise only has Kimi via NIM.
    # Multimodal (image input) on OpenRouter, native tools + reasoning.
    register(ModelConfig(
        name="moonshotai/kimi-k2.5",
        provider=ModelProvider.OPENROUTER,
        context_window=262144,
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.375, output_price=2.025),
        capabilities=ModelCapabilities(
            supports_vision=True, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2025-10",
        aliases=["kimi-k2.5", "kimi-k25", "or-kimi"]
    ))

    # MiniMax M2 — strong agentic-coding MoE; prompt-cache read priced.
    register(ModelConfig(
        name="minimax/minimax-m2",
        provider=ModelProvider.OPENROUTER,
        context_window=204800,
        max_completion_tokens=131072,  # models.dev openrouter limit.output (2026-08-15)
        pricing=ModelPricing(input_price=0.255, cached_input_price=0.03, output_price=1.0),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2025-09",
        aliases=["minimax-m2", "minimax2"]
    ))

    # GLM-4.6 — cheaper GLM tier below 5.2; native tools + reasoning, cache read priced.
    register(ModelConfig(
        name="z-ai/glm-4.6",
        provider=ModelProvider.OPENROUTER,
        context_window=202752,
        max_completion_tokens=131072,
        pricing=ModelPricing(input_price=0.43, cached_input_price=0.08, output_price=1.74),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
            thinking_budget_tokens=32000,
        ),
        knowledge_cutoff="2025-12",
        aliases=["glm-4.6", "glm4.6"]
    ))

    # Qwen3 Coder 30B-A3B — cheap dedicated coder, native tools.
    register(ModelConfig(
        name="qwen/qwen3-coder-30b-a3b-instruct",
        provider=ModelProvider.OPENROUTER,
        context_window=160000,
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.07, output_price=0.27),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True, supports_json_mode=True,
        ),
        knowledge_cutoff="2025-07",
        aliases=["qwen3-coder-30b", "qwen3-coder-flash"]
    ))

    # --- Cheap high-volume workhorses ---

    # DeepSeek V3.2 — latest DeepSeek, very cheap output, native tools + reasoning.
    register(ModelConfig(
        name="deepseek/deepseek-v3.2",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=0.2288, output_price=0.3432),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2025-07",
        aliases=["deepseek-v3.2", "or-deepseek-v3.2"]
    ))

    # DeepSeek V4 Flash — cheapest capable + 1M context, cache read priced.
    register(ModelConfig(
        name="deepseek/deepseek-v4-flash",
        provider=ModelProvider.OPENROUTER,
        context_window=1048576,
        max_completion_tokens=65536,
        pricing=ModelPricing(input_price=0.09, cached_input_price=0.02, output_price=0.18),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2025-12",
        aliases=["deepseek-v4-flash", "deepseek-flash"]
    ))

    # Llama 3.3 70B Instruct — reliable open workhorse, native tools.
    register(ModelConfig(
        name="meta-llama/llama-3.3-70b-instruct",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=0.1, output_price=0.32),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True, supports_json_mode=True,
        ),
        knowledge_cutoff="2024-12",
        aliases=["llama-3.3-70b", "llama3.3-70b"]
    ))

    # Mistral Small 3.2 24B — cheap European option, multimodal (image input).
    register(ModelConfig(
        name="mistralai/mistral-small-3.2-24b-instruct",
        provider=ModelProvider.OPENROUTER,
        context_window=128000,
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=0.075, output_price=0.2),
        capabilities=ModelCapabilities(
            supports_vision=True, supports_function_calling=True,
            supports_tools=True, supports_streaming=True, supports_json_mode=True,
        ),
        knowledge_cutoff="2025-06",
        aliases=["mistral-small-3.2", "mistral-small"]
    ))

    # Qwen3 30B-A3B Instruct — very cheap qwen workhorse, native tools.
    register(ModelConfig(
        name="qwen/qwen3-30b-a3b-instruct-2507",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,
        max_completion_tokens=32000,
        pricing=ModelPricing(input_price=0.04815, output_price=0.193),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True, supports_json_mode=True,
        ),
        knowledge_cutoff="2025-07",
        aliases=["qwen3-30b-a3b", "qwen3-30b"]
    ))

    # --- Open-weights / OSS ---

    # OpenAI gpt-oss-120b — OpenAI open-weights, extremely cheap, native tools + reasoning.
    register(ModelConfig(
        name="openai/gpt-oss-120b",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.039, output_price=0.18),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2024-06",
        aliases=["gpt-oss-120b", "gpt-oss"]
    ))

    # Nous Hermes-4 70B (open model via OpenRouter). NOTE: OpenRouter does NOT
    # advertise the `tools` parameter for Hermes-4, so supports_tools=False →
    # the agent uses the JSON-from-text fallback (not native tool calls).
    register(ModelConfig(
        name="nousresearch/hermes-4-70b",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=0.13, output_price=0.4),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=False,
            supports_tools=False, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2024-12",
        aliases=["hermes-4-70b", "hermes4-70b"]
    ))

    # Nous Hermes-4 405B (larger variant). Same no-native-tools caveat as 70B.
    register(ModelConfig(
        name="nousresearch/hermes-4-405b",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=1.0, output_price=3.0),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=False,
            supports_tools=False, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2024-12",
        aliases=["hermes-4-405b", "hermes4-405b", "hermes"]
    ))

    # --- Vision / long-context ---

    # Qwen3-VL 8B Instruct — cheap vision (image input), native tools.
    register(ModelConfig(
        name="qwen/qwen3-vl-8b-instruct",
        provider=ModelProvider.OPENROUTER,
        context_window=256000,
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.08, output_price=0.5),
        capabilities=ModelCapabilities(
            supports_vision=True, supports_function_calling=True,
            supports_tools=True, supports_streaming=True, supports_json_mode=True,
        ),
        knowledge_cutoff="2025-07",
        aliases=["qwen3-vl-8b", "qwen-vl-8b"]
    ))

    # Llama 4 Scout — extreme long-context (10M), multimodal, native tools.
    register(ModelConfig(
        name="meta-llama/llama-4-scout",
        provider=ModelProvider.OPENROUTER,
        context_window=10000000,
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=0.1, output_price=0.3),
        capabilities=ModelCapabilities(
            supports_vision=True, supports_function_calling=True,
            supports_tools=True, supports_streaming=True, supports_json_mode=True,
        ),
        knowledge_cutoff="2024-08",
        aliases=["llama-4-scout", "llama4-scout"]
    ))

    # MiniMax M3 — long-context (1M) agentic, multimodal, reasoning, cache read priced.
    register(ModelConfig(
        name="minimax/minimax-m3",
        provider=ModelProvider.OPENROUTER,
        context_window=1048576,
        max_completion_tokens=512000,
        pricing=ModelPricing(input_price=0.3, cached_input_price=0.06, output_price=1.2),
        capabilities=ModelCapabilities(
            supports_vision=True, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2025-11",
        aliases=["minimax-m3", "minimax3"]
    ))
