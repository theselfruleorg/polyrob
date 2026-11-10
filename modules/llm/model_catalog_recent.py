"""Recent built-in rows, split to preserve the model catalog size ceiling.
A price here is an estimate; ``billed_cost_usd`` is the spend truth."""
from modules.llm.model_types import ModelCapabilities, ModelConfig, ModelPricing, ModelProvider


def register_recent_models(register) -> None:
    """Register these rows through the registry's existing callback."""
    # Llama 4 Scout — long-context (1.3M), multimodal, native tools.
    register(ModelConfig(
        name="meta-llama/llama-4-scout",
        provider=ModelProvider.OPENROUTER,
        context_window=1310720,
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

    # DeepSeek V4 Pro via OpenRouter, text only. Price = StreamLake, the live list row;
    # upstreams range up to 1.91/0.16/3.83, so billed_cost_usd is the truth here.
    register(ModelConfig(
        name="deepseek/deepseek-v4-pro",
        provider=ModelProvider.OPENROUTER,
        context_window=1048576,
        max_completion_tokens=384000,
        pricing=ModelPricing(input_price=0.7282, cached_input_price=0.0607, output_price=1.4564),
        capabilities=ModelCapabilities(
            supports_vision=False, supports_function_calling=True,
            supports_tools=True, supports_streaming=True,
            supports_json_mode=True, supports_thinking=True,
        ),
        aliases=["or-deepseek-v4-pro"]
    ))

    # Claude Opus 5.5 - the current Opus. Thinking cannot be disabled; effort default medium.
    register(ModelConfig(
        name="claude-opus-5-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=4.00, cached_input_price=0.20, output_price=20.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # adaptive; budget_tokens rejected
            supports_mid_conversation_tool_changes=True,
            preserves_thinking=True,
            reasoning_effort="medium",  # the docs' start for this model, and its default
        ),
        chars_per_token=5.0,
        aliases=["claude-opus-5.5", "opus-5.5", "opus-5-5"]
    ))

    # Claude Sonnet 5.5 - the current Sonnet and the anthropic default.
    register(ModelConfig(
        name="claude-sonnet-5-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=2.00, cached_input_price=0.20, output_price=10.00),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # adaptive on by default; budget_tokens rejected
            supports_mid_conversation_tool_changes=True,
            preserves_thinking=True,
            reasoning_effort="medium",  # the docs' start for agentic tool use; the API default is high
        ),
        chars_per_token=5.0,
        aliases=["claude-sonnet-5.5", "sonnet-5.5", "sonnet-5-5"]
    ))

    # Claude Haiku 5.5 - the current Haiku. The price is an ESTIMATE: this rate holds up
    # to a 100K prompt, and a longer prompt bills at $0.50/$2.50. Cache read is 0.1x input.
    register(ModelConfig(
        name="claude-haiku-5-5",
        provider=ModelProvider.ANTHROPIC,
        context_window=1000000,
        max_completion_tokens=128000,
        pricing=ModelPricing(input_price=0.10, cached_input_price=0.01, output_price=0.50),
        capabilities=ModelCapabilities(
            supports_vision=True,
            supports_thinking=True,  # adaptive only; budget_tokens is a 400
            preserves_thinking=True,
            reasoning_effort="medium",  # the docs' start for this model, and its default
        ),
        chars_per_token=5.0,
        aliases=["claude-haiku-5.5", "haiku-5.5", "haiku-5-5"]
    ))
