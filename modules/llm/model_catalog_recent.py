"""Recent built-in rows, split to preserve the model catalog size ceiling."""
from modules.llm.model_types import ModelCapabilities, ModelConfig, ModelPricing, ModelProvider


def register_recent_models(register) -> None:
    """Register unchanged catalog rows through the registry's existing callback."""
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
