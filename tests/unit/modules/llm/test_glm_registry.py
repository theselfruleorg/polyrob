"""GLM (Z.AI) via OpenRouter — registry registration, aliases, fallback, default.

GLM runs through the existing ``openrouter`` provider (OpenAI-compatible schema +
OpenRouterClient), so the only integration gap is the model metadata. These tests
pin the registered specs, the alias/fallback resolution (so an unknown ``glm`` id
never silently falls back to gpt-5.1's config), and the OpenRouter default.
"""
from modules.llm.model_registry import get_model_config, get_thinking_config
from modules.llm.llm_client_registry import DEFAULT_MODELS, get_default_model
from modules.llm.model_registry import ModelProvider


def test_glm_5_2_registered_with_correct_specs():
    cfg = get_model_config("z-ai/glm-5.2")
    assert cfg is not None
    assert cfg.name == "z-ai/glm-5.2"
    assert cfg.provider == ModelProvider.OPENROUTER
    assert cfg.context_window == 1048576
    assert cfg.max_completion_tokens == 262144
    assert cfg.pricing.input_price == 1.20  # OpenRouter API verified 2026-06-20
    assert cfg.pricing.output_price == 4.10
    assert cfg.capabilities.supports_function_calling is True
    assert cfg.capabilities.supports_tools is True
    assert cfg.capabilities.supports_json_mode is True
    assert cfg.capabilities.supports_streaming is True
    assert cfg.capabilities.supports_vision is False


def test_glm_5_registered():
    cfg = get_model_config("z-ai/glm-5")
    assert cfg is not None and cfg.name == "z-ai/glm-5"
    assert cfg.provider == ModelProvider.OPENROUTER
    assert cfg.context_window == 202752
    assert cfg.pricing.input_price == 0.60
    assert cfg.pricing.output_price == 1.92
    assert cfg.capabilities.supports_function_calling is True
    assert cfg.capabilities.supports_vision is False
    assert get_model_config("glm-5").name == "z-ai/glm-5"


def test_glm_4_7_registered():
    cfg = get_model_config("z-ai/glm-4.7")
    assert cfg is not None and cfg.name == "z-ai/glm-4.7"
    assert cfg.provider == ModelProvider.OPENROUTER
    assert cfg.context_window == 202752
    assert cfg.max_completion_tokens == 131072
    assert cfg.pricing.input_price == 0.40
    assert cfg.pricing.output_price == 1.75
    assert cfg.capabilities.supports_function_calling is True
    assert cfg.capabilities.supports_vision is False
    assert get_model_config("glm-4.7").name == "z-ai/glm-4.7"


def test_glm_aliases_resolve():
    for alias in ("glm", "glm-5.2", "glm5.2"):
        cfg = get_model_config(alias)
        assert cfg is not None and cfg.name == "z-ai/glm-5.2", alias


def test_unknown_glm_id_falls_back_to_glm_not_gpt():
    # A future/unknown GLM id must resolve to a GLM config, never gpt-5.1's.
    cfg = get_model_config("z-ai/glm-9.9-imaginary")
    assert cfg is not None
    assert cfg.provider == ModelProvider.OPENROUTER
    assert cfg.name.startswith("z-ai/glm")


def test_openrouter_default_is_glm():
    assert DEFAULT_MODELS["openrouter"] == "z-ai/glm-5.2"
    assert get_default_model("openrouter") == "z-ai/glm-5.2"


def test_glm_thinking_config_present_when_capable():
    # supports_thinking=True with a budget => get_thinking_config returns it
    # (only consumed when THINKING_CONFIG_ENABLED; default off).
    cfg = get_thinking_config("z-ai/glm-5.2")
    assert cfg.get("budget_tokens")
