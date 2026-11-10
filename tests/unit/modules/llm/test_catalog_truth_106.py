"""106 W2: the catalog holds a live row for every model production runs, and no dead row."""
import pytest

from modules.llm.model_registry import get_model_config_exact


def test_openrouter_deepseek_v4_pro_has_an_exact_row():
    cfg = get_model_config_exact("deepseek/deepseek-v4-pro")
    assert cfg is not None
    assert (cfg.context_window, cfg.max_completion_tokens) == (1048576, 384000)
    assert cfg.pricing.input_price == 0.7282
    assert cfg.capabilities.supports_vision is False


def test_glm_53_flash_reads_the_price_most_upstreams_charge():
    cfg = get_model_config_exact("z-ai/glm-5.3-flash")
    assert (cfg.pricing.input_price, cfg.pricing.cached_input_price,
            cfg.pricing.output_price) == (0.15, 0.03, 0.5)
    assert cfg.context_window == 1048576


@pytest.mark.parametrize("dead", [
    "x-ai/grok-4.1-fast", "x-ai/grok-4-fast", "x-ai/grok-4", "x-ai/grok-code-fast-1",
    "x-ai/grok-3", "x-ai/grok-3-mini", "qwen/qwen3-235b-a22b", "qwen/qwen3-max",
    "deepseek/deepseek-r1", "deepseek/deepseek-r1-distill-qwen-32b",
    "minimax/minimax-m2", "nousresearch/hermes-4-70b",
])
def test_dead_rows_are_gone(dead):
    assert get_model_config_exact(dead) is None


def test_every_family_fallback_target_exists():
    from modules.llm.model_registry import get_model_config
    assert get_model_config("qwen/qwen9-imaginary").name == "qwen/qwen3-coder"
    assert get_model_config("deepseek/deepseek-v9").name == "deepseek/deepseek-v4.1-flash"
    assert get_model_config("deepseek-v9").name == "deepseek-flash"


@pytest.mark.parametrize("model", ["MiniMax-M2.7", "step-3.5-flash", "hy3", "no-such-model"])
def test_a_model_with_no_row_never_reads_another_family(model):
    from modules.llm.model_registry import get_model_config
    cfg = get_model_config(model)
    assert cfg.name == model
    assert cfg.capabilities.supports_vision is False
    assert (cfg.context_window, cfg.max_completion_tokens) == (128000, 8192)


def test_a_spec_that_declares_vision_gives_the_no_row_model_its_vision(monkeypatch):
    import modules.llm.provider_spec as ps
    from modules.llm.model_registry import get_model_config
    from dataclasses import replace
    row = replace(ps.BUILTIN_SPECS[0], name="seat", models=("seat-model",), supports_vision=True)
    monkeypatch.setattr(ps, "get_specs", lambda: (row,))
    assert get_model_config("seat-model").capabilities.supports_vision is True
    assert get_model_config("other-model").capabilities.supports_vision is False


def test_a_substring_in_a_name_is_not_a_family():
    from modules.llm.model_registry import get_model_config
    assert get_model_config("vendor/hello1-pro").name == "vendor/hello1-pro"
    assert get_model_config("o3-next").name == "gpt-5.1"


def test_a_model_with_no_row_has_no_price_for_billing():
    from modules.llm.model_registry import calculate_cost, get_model_config_exact
    assert get_model_config_exact("MiniMax-M2.7") is None
    assert calculate_cost("MiniMax-M2.7", 1000, 1000) == 0.0


def test_the_unknown_id_keeps_a_native_tool_schema():
    """An unknown OpenRouter slug must not lose native tool calling."""
    from tools.controller.registry.schema_generators import supports_native_tools
    from modules.llm.usage_extract import detect_llm_provider
    provider = detect_llm_provider("mistralai/some-new-model")
    assert provider == "openrouter" and supports_native_tools(provider)


def test_a_spec_only_provider_lists_its_declared_models(monkeypatch):
    import modules.llm.available_models as am
    monkeypatch.setattr(am, "usable_providers_with_credentials", lambda env=None: ["zai-coding"])
    choices = {c.model: c for c in am.available_models()}
    assert "glm-5.3" in choices and "glm-5.3-flash" in choices
    assert choices["glm-5.3"].is_default is True
    # the alias row gives the capability, and its metered price is not this seat's price
    assert choices["glm-5.3-flash"].supports_vision is True
    assert choices["glm-5.3"].pricing_hint == "(pricing n/a)"


def test_a_spec_model_with_no_row_is_listed_with_the_spec_capabilities(monkeypatch):
    import modules.llm.available_models as am
    monkeypatch.setattr(am, "usable_providers_with_credentials", lambda env=None: ["stepfun"])
    choices = {c.model: c for c in am.available_models()}
    assert choices["step-3.5-flash"].context_window == 0
    assert choices["step-3.5-flash"].supports_vision is False


def test_cached_tokens_with_no_cached_price_cost_the_input_price(monkeypatch):
    from modules.llm import model_registry as mr
    from modules.llm.model_types import (ModelCapabilities, ModelConfig, ModelPricing,
                                         ModelProvider)
    row = ModelConfig(name="no-cache-price", provider=ModelProvider.OPENROUTER,
                      context_window=1000, max_completion_tokens=10,
                      pricing=ModelPricing(input_price=1.0, output_price=0.0),
                      capabilities=ModelCapabilities())
    row.pricing.cached_input_price = None
    monkeypatch.setitem(mr.get_registry()._models, row.name, row)
    assert mr.calculate_cost(row.name, 1_000_000, 0, cached_tokens=1_000_000) == 1.0


def test_a_live_row_with_no_cached_price_bills_a_hit_as_input(monkeypatch):
    from modules.llm import model_registry as mr
    from modules.llm.openrouter_catalog import CatalogEntry, register_live_model
    name = "vendor/live-no-cache-price"
    monkeypatch.delitem(mr.get_registry()._models, name, raising=False)
    register_live_model(CatalogEntry(id=name, input_price=2.0, output_price=4.0,
                                     cached_price=None, context=1000, tools=True, reasoning=False))
    try:
        assert mr.get_model_config_exact(name).pricing.cached_input_price == 2.0
    finally:
        mr.get_registry()._models.pop(name, None)


def _map_model_with_usable(monkeypatch, usable):
    monkeypatch.setattr("modules.llm.profiles.usable_providers_with_credentials",
                        lambda env=None: usable)
    from api.openai_compat.model_map import map_model
    return map_model


def test_an_openrouter_slug_goes_to_its_catalog_owner(monkeypatch):
    map_model = _map_model_with_usable(monkeypatch, ["openrouter", "deepseek", "openai"])
    assert map_model("deepseek/deepseek-v4-pro") == ("openrouter", "deepseek/deepseek-v4-pro")
    assert map_model("openai/gpt-oss-120b") == ("openrouter", "openai/gpt-oss-120b")


def test_a_slug_with_no_owner_key_keeps_the_head_that_has_a_key(monkeypatch):
    map_model = _map_model_with_usable(monkeypatch, ["deepseek"])
    assert map_model("deepseek/deepseek-v4-pro") == ("deepseek", "deepseek-v4-pro")
