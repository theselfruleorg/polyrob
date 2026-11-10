"""Built-in model CATALOG — the data half of the model registry.

``ModelRegistry._initialize_models`` calls ``register_builtin_models`` with its
``_register_model`` bound method; ``catalog_lookup`` reads the same rows with no registry.
Pricing math lives in ``model_pricing``; the
enum/dataclasses in ``model_types``. ``devtools/catalog_diff.py`` checks the OpenRouter
rows against the live list. A catalog price is an estimate; ``billed_cost_usd`` is the
spend truth.
"""
from typing import Callable

from modules.llm.model_types import ModelCapabilities, ModelConfig, ModelPricing, ModelProvider


def register_builtin_models(register: Callable[[ModelConfig], None]) -> None:
    """Register every built-in model through *register* (one ``ModelConfig`` per call)."""

    # OPENAI MODELS

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
    # ========================================

    # ----------------------------------------
    # CURRENT LINEUP (Fable 5 / Opus 4.6-4.8 / Sonnet 4.6-5); Opus 5.5, Sonnet 5.5
    # and Haiku 5.5 are in model_catalog_recent.py.
    # All 1M-context natively (no beta header) and 128K max output. Extended
    # thinking is ADAPTIVE on these models — `budget_tokens` is rejected (400)
    # on Fable 5 / Opus 4.7-4.8 / Sonnet 5 and deprecated on Opus 4.6 /
    # Sonnet 4.6 — so `thinking_budget_tokens` is left unset (adaptive/effort
    # is the control, get_thinking_config() returns {} => no thinking params
    # sent). max_completion_tokens kept at the 64000 family convention on rows
    # older than Fable 5.1. Pricing from the claude-api model catalog.
    # ----------------------------------------

    # `supports_mid_conversation_tool_changes` is the
    # ONE table that answers "may this model be sent `defer_loading` tools and
    # `tool_addition` messages" (beta mid-conversation-tool-changes-2026-07-01).
    # Available on Opus 5.5, Opus 5, Opus 4.8, Fable 5, Fable 5.1 and Sonnet 5.5
    # (claude-api prompt-caching reference). NOT on Sonnet 5, Sonnet 4.6, Opus 4.7
    # or older.

    # Claude Fable 5 - thinking always on, cache read $1/MTok.
    register(ModelConfig(
        name="claude-fable-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=10.00, cached_input_price=1.00, output_price=50.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # always-on adaptive; budget_tokens rejected
            supports_mid_conversation_tool_changes=True,
        ),
        chars_per_token=5.0,
        aliases=["claude-fable", "fable-5", "fable", "claude-fable5"]
    ))

    # Claude Fable 5.1 - 128K max output; its cache READ is a flat $0.25/MTok,
    # NOT the 0.1x the derivation would produce, so the price is explicit here.
    register(ModelConfig(
        name="claude-fable-5-1",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=128000,
        pricing=ModelPricing(
            input_price=10.00, output_price=50.00, cached_input_price=0.25),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # always-on adaptive; budget_tokens rejected
            supports_mid_conversation_tool_changes=True, preserves_thinking=True,
        ),
        chars_per_token=5.0,
        aliases=["claude-fable-5.1", "fable-5-1", "fable-5.1"]
    ))

    # Claude Opus 5 - the fallback target for any unknown `claude-opus-*` id
    # (family chain in model_registry.py).
    register(ModelConfig(
        name="claude-opus-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=5.00, cached_input_price=0.50, output_price=25.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # adaptive; budget_tokens rejected (400)
            supports_mid_conversation_tool_changes=True,
        ),
        chars_per_token=5.0,
        aliases=["claude-opus5", "opus-5", "claude-5-opus"]
    ))

    # Claude Opus 4.8
    register(ModelConfig(
        name="claude-opus-4-8",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=5.00, cached_input_price=0.50, output_price=25.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # adaptive; budget_tokens rejected (400)
            supports_mid_conversation_tool_changes=True,
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
            supports_vision=True,
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
            supports_vision=True,
            supports_thinking=True,  # adaptive (budget_tokens deprecated here)
        ),
        chars_per_token=5.0,
        aliases=["claude-opus-4.6", "opus-4.6", "opus-4-6"]
    ))

    # Claude Sonnet 5 - the previous Sonnet.
    register(ModelConfig(
        name="claude-sonnet-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=64000,
        pricing=ModelPricing(input_price=2.00, cached_input_price=0.20, output_price=10.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
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
            supports_vision=True,
            supports_thinking=True,  # adaptive (budget_tokens deprecated here)
        ),
        chars_per_token=5.0,
        aliases=["claude-sonnet-4.6", "sonnet-4.6", "sonnet-4-6"]
    ))

    # ----------------------------------------
    # LEGACY LINEUP (claude-sonnet-4-5 retires 2026-11-30)
    # ----------------------------------------

    # Claude 4.5 Series
    register(ModelConfig(
        name="claude-sonnet-4-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=200000,  # 1M with beta header
        max_completion_tokens=64000,  # Anthropic max is 64000
        pricing=ModelPricing(input_price=3.00, output_price=15.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # Extended thinking supported
            thinking_budget_tokens=64000,
        ),
        chars_per_token=5.0,
        knowledge_cutoff="2025-01",
        aliases=["claude-sonnet-4.5", "claude-4.5-sonnet", "claude-sonnet-4-5-20250929"]
    ))

    register(ModelConfig(
        name="claude-haiku-4-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=200000,
        max_completion_tokens=64000,  # Anthropic max is 64000
        pricing=ModelPricing(input_price=1.00, output_price=5.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # Extended thinking supported
            thinking_budget_tokens=64000,
        ),
        chars_per_token=5.0,
        knowledge_cutoff="2025-02",
        aliases=["claude-haiku-4.5", "claude-4.5-haiku", "claude-haiku-4-5-20251001"]
    ))

    # Claude Opus 4.5
    register(ModelConfig(
        name="claude-opus-4-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=200000,
        max_completion_tokens=64000,  # Anthropic max is 64000
        pricing=ModelPricing(input_price=5.00, output_price=25.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # Extended thinking supported
            thinking_budget_tokens=64000,
        ),
        chars_per_token=5.0,
        knowledge_cutoff="2025-03",
        aliases=["claude-opus-4.5", "claude-4.5-opus", "claude-opus-4-5-20251101"]
    ))

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
    # DEEPSEEK MODELS — DeepSeek's own API (verified)
    # Source: https://api-docs.deepseek.com/quick_start/pricing + GET /models
    # Prices are OFF-PEAK; weekdays 01:00-04:00 and 06:00-10:00 UTC bill 2x —
    # DeepSeekClient reports the exact charge as billed_cost_usd.
    # Thinking is ON by default (effort high); tool calls work in both modes.
    # ========================================

    # DeepSeek V4.1 Flash. The retired ids still answer and are served by this
    # model: `deepseek-chat` = thinking OFF, `deepseek-reasoner` = thinking ON.
    register(ModelConfig(
        name="deepseek-flash",
        provider=ModelProvider.DEEPSEEK,
        display_name_override="DeepSeek V4.1 Flash",
        context_window=1048576,
        max_completion_tokens=393216,
        pricing=ModelPricing(input_price=0.15, cached_input_price=0.003, output_price=0.6),
        capabilities=ModelCapabilities(
            supports_vision=True, supports_function_calling=True, supports_tools=True,
            supports_streaming=True, supports_json_mode=True, supports_thinking=True,
        ),
        aliases=["deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash",
                 "deepseek-v4.1-flash", "deepseek-4.1-flash"],
        served_aliases=("deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash"),
    ))

    # DeepSeek V4 Pro — the larger model, text only.
    register(ModelConfig(
        name="deepseek-v4-pro",
        provider=ModelProvider.DEEPSEEK,
        display_name_override="DeepSeek V4 Pro",
        context_window=1048576,
        max_completion_tokens=393216,
        pricing=ModelPricing(input_price=0.66, cached_input_price=0.022, output_price=1.98),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True, supports_tools=True,
            supports_streaming=True, supports_json_mode=True, supports_thinking=True,
        ),
        aliases=["deepseek-pro"]
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

    # GLM-5.3 - newest Z.AI flagship (1M ctx, 128K max output). Native tools +
    # reasoning + structured outputs, TEXT-ONLY (verified live: given a
    # base64 image on the z.ai Anthropic endpoint it narrated a URL it could not
    # fetch rather than reading the pixels — so vision here would be a hallucination
    # licence, not a capability). Price = DeepInfra, the first upstream in the production
    # order; Z.AI and most others charge 1.40/0.26/4.40.
    register(ModelConfig(
        name="z-ai/glm-5.3",
        provider=ModelProvider.OPENROUTER,
        context_window=1048576,
        max_completion_tokens=131072,  # z.ai rejects max_tokens>131072 (live probe)
        pricing=ModelPricing(input_price=0.5625, cached_input_price=0.125, output_price=2.5),
        capabilities=ModelCapabilities(
            supports_vision=False,  # text->text
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True,
            reasoning_mandatory=True,
            thinking_budget_tokens=32000,
        ),
        knowledge_cutoff="2026-06",
        aliases=["glm-5.3", "glm5.3"]
    ))

    # GLM-5.3-Flash - the cheap tier of the 5.3 generation and POLYROB's aux model
    # on a z.ai seat (the spec's aux_model). 320B MoE / 18B active, same 1M ctx. Price = most
    # upstreams, DeepInfra and Z.AI included.
    # ⚠️ Unlike every other GLM row here it IS multimodal (text/image/video in) —
    # live-verified on api.z.ai/api/anthropic, where it read an 8x8 PNG
    # correctly while GLM-5.3 could not. It is the only vision-capable model a
    # GLM-Coding-Plan deployment has.
    register(ModelConfig(
        name="z-ai/glm-5.3-flash",
        provider=ModelProvider.OPENROUTER,
        context_window=1048576,
        max_completion_tokens=131072,  # z.ai rejects max_tokens>131072 (live probe)
        pricing=ModelPricing(input_price=0.15, cached_input_price=0.03, output_price=0.5),
        capabilities=ModelCapabilities(
            supports_vision=True,  # image + video input (z.ai VLM row)
            supports_function_calling=True,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_thinking=True,
            thinking_budget_tokens=32000,
        ),
        knowledge_cutoff="2026-06",
        aliases=["glm-5.3-flash", "glm5.3-flash"]
    ))

    # GLM-5.2 - current Z.AI flagship on OpenRouter (default for `--provider
    # openrouter`). 1M context, native tools + reasoning + structured outputs,
    # text-only. OpenAI-compatible via OpenRouter (no client change needed).
    # Specs/pricing verified against the live OpenRouter models API.
    register(ModelConfig(
        name="z-ai/glm-5.2",
        provider=ModelProvider.OPENROUTER,
        context_window=1048576,  # 1.05M tokens (OpenRouter API is authoritative)
        max_completion_tokens=32768,  # OpenRouter top_provider cap (re-verified)
        pricing=ModelPricing(input_price=0.93, cached_input_price=0.18, output_price=3.00),  # OpenRouter models API
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

    # GLM-5 - balanced Gen-5 GLM (203K ctx). Native tools + reasoning + structured
    # outputs, text-only. Price = Z.AI, the live list row.
    register(ModelConfig(
        name="z-ai/glm-5",
        provider=ModelProvider.OPENROUTER,
        context_window=202752,  # 203K tokens
        max_completion_tokens=131072,  # family cap (OpenRouter API authoritative)
        pricing=ModelPricing(input_price=1.0, cached_input_price=0.2, output_price=3.2),
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
        pricing=ModelPricing(input_price=0.4, cached_input_price=0.11, output_price=1.75),
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
    # reasoning + structured outputs, text-only. Price = Z.AI, the live list row.
    register(ModelConfig(
        name="z-ai/glm-5.1",
        provider=ModelProvider.OPENROUTER,
        context_window=202752,  # 203K tokens
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=1.4, cached_input_price=0.26, output_price=4.4),
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

    # GLM-5-Turbo - faster Gen-5 GLM (203K ctx). Native tools + reasoning +
    # structured outputs, text-only.
    register(ModelConfig(
        name="z-ai/glm-5-turbo",
        provider=ModelProvider.OPENROUTER,
        context_window=202752,
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
    # Native tools; reasoning off on the flash tier. Verified.
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

    # Grok 4.3 - xAI on OpenRouter, the OpenRouter API enforces the limits.
    register(ModelConfig(
        name="x-ai/grok-4.3",
        provider=ModelProvider.OPENROUTER,
        context_window=1000000,  # 1M tokens (OpenRouter re-verified)
        max_completion_tokens=30000,
        pricing=ModelPricing(input_price=1.25, cached_input_price=0.20, output_price=2.50),  # OpenRouter models API
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

    # Grok 4.5 - newest xAI flagship on OpenRouter. 500K ctx, multimodal, native
    # tools + reasoning.
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
        max_completion_tokens=98304,
        pricing=ModelPricing(input_price=0.6, output_price=2.5),
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
        max_completion_tokens=98304,
        pricing=ModelPricing(input_price=0.57, output_price=2.3),
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
        pricing=ModelPricing(input_price=0.6, cached_input_price=0.15, output_price=2.5),
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

    # Qwen3 Coder 480B A35B - Code generation specialist
    # Created: Jul 23, 2025 | 480B total, 35B active (8/160 experts)
    register(ModelConfig(
        name="qwen/qwen3-coder",
        provider=ModelProvider.OPENROUTER,
        context_window=262144,  # 262K tokens - verified
        max_completion_tokens=65536,  # models.dev openrouter limit.output
        pricing=ModelPricing(input_price=0.3, cached_input_price=0.1, output_price=1.0),
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
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=0.09, cached_input_price=0.09, output_price=0.28),
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
        pricing=ModelPricing(input_price=0.2, cached_input_price=0.1, output_price=1.2),
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
    # ----------------------------------------

    # DeepSeek Chat V3 via OpenRouter.
    register(ModelConfig(
        name="deepseek/deepseek-chat",
        provider=ModelProvider.OPENROUTER,
        context_window=163840,
        max_completion_tokens=8192,  # 8K output
        pricing=ModelPricing(input_price=0.32, output_price=0.89),
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

    # ========================================
    # CURATED OPENROUTER SHORTLIST
    # Models POLYROB has no native client for (OpenRouter's real value). Pinned in
    # tests/unit/modules/llm/test_openrouter_pricing_verified.py.
    # ========================================

    # --- Frontier agentic / coding ---

    # Moonshot Kimi K2.5 — strong agentic; POLYROB otherwise only has Kimi via NIM.
    # Multimodal (image input) on OpenRouter, native tools + reasoning.
    register(ModelConfig(
        name="moonshotai/kimi-k2.5",
        provider=ModelProvider.OPENROUTER,
        context_window=262144,
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.5, cached_input_price=0.15, output_price=2.5),
        capabilities=ModelCapabilities(
            supports_vision=True, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2025-10",
        aliases=["kimi-k2.5", "kimi-k25", "or-kimi"]
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
        context_window=262144,
        max_completion_tokens=32768,
        pricing=ModelPricing(input_price=0.07, output_price=0.6),
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
        context_window=163840,
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
        aliases=["or-deepseek-v4-flash"]
    ))

    # DeepSeek V4.1 Flash — the cheap 1M-context workhorse, and it SEES: OpenRouter
    # reports modality text+image->text, unlike every other DeepSeek row here.
    # ⚠️ Time-of-day pricing on DeepSeek's own upstream: on weekdays 01:00-04:00 and 06:00-10:00 UTC the
    # rate DOUBLES to 0.30/1.20 (cache read 0.006). Cost telemetry computed from this row
    # is therefore a FLOOR during those windows, not an exact charge — re-check against
    # `GET https://openrouter.ai/api/v1/models` before quoting a spend figure from it.
    # Priced to the OpenRouter list (DeepInfra); billed_cost_usd is the
    # spend truth and `polyrob doctor` prints the billed/catalog ratio.
    register(ModelConfig(
        name="deepseek/deepseek-v4.1-flash",
        provider=ModelProvider.OPENROUTER,
        context_window=1048576,
        max_completion_tokens=384000,
        pricing=ModelPricing(input_price=0.14, cached_input_price=0.004, output_price=0.42),
        capabilities=ModelCapabilities(
            supports_vision=True, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        aliases=["or-deepseek-v4.1-flash"]
    ))

    # Llama 3.3 70B Instruct — reliable open workhorse, native tools.
    register(ModelConfig(
        name="meta-llama/llama-3.3-70b-instruct",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=0.1, cached_input_price=0.1, output_price=0.32),
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
        context_window=256000,
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=0.075, cached_input_price=0.01, output_price=0.2),
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
        context_window=262144,
        max_completion_tokens=32000,
        pricing=ModelPricing(input_price=0.1, cached_input_price=0.1, output_price=0.3),
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
        pricing=ModelPricing(input_price=0.039, cached_input_price=0.015, output_price=0.18),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        knowledge_cutoff="2024-06",
        aliases=["gpt-oss-120b", "gpt-oss"]
    ))

    # Nous Hermes-4 405B. OpenRouter does NOT advertise the `tools` parameter, so
    # supports_tools=False: the agent uses the JSON-from-text fallback.
    register(ModelConfig(
        name="nousresearch/hermes-4-405b",
        provider=ModelProvider.OPENROUTER,
        context_window=131072,
        max_completion_tokens=16384,
        pricing=ModelPricing(input_price=1.0, cached_input_price=0.2, output_price=3.0),
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
        pricing=ModelPricing(input_price=0.25, cached_input_price=0.12, output_price=0.75),
        capabilities=ModelCapabilities(
            supports_vision=True, supports_function_calling=True,
            supports_tools=True, supports_streaming=True, supports_json_mode=True,
        ),
        knowledge_cutoff="2025-07",
        aliases=["qwen3-vl-8b", "qwen-vl-8b"]
    ))

    from modules.llm.model_catalog_recent import register_recent_models
    register_recent_models(register)
