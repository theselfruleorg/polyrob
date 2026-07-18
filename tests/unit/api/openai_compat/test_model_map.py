from api.openai_compat.model_map import map_model


def test_slug_with_provider_prefix():
    assert map_model("anthropic/claude-sonnet-4-5") == ("anthropic", "claude-sonnet-4-5")


def test_openrouter_nested_slug_splits_on_first_slash():
    assert map_model("openrouter/z-ai/glm-5.2") == ("openrouter", "z-ai/glm-5.2")


def test_bare_gpt_maps_to_openai():
    assert map_model("gpt-4") == ("openai", "gpt-4")


def test_bare_claude_maps_to_anthropic():
    prov, model = map_model("claude-3.5-sonnet")
    assert prov == "anthropic" and model == "claude-3.5-sonnet"


def test_unknown_falls_back_to_default_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    prov, model = map_model("rob-default")
    assert prov in ("anthropic", "openai", "gemini", "deepseek", "openrouter", "nvidia")
    assert model == "rob-default"


def test_non_provider_head_slug_falls_through_to_prefix():
    # head "gpt-4" is not a known provider, so the slug isn't split; it falls
    # through to prefix matching ("gpt" -> openai), model kept verbatim.
    assert map_model("gpt-4/turbo") == ("openai", "gpt-4/turbo")


def test_bare_registered_slug_resolves_via_registry():
    """WS-7: a bare registered model slug whose vendor prefix (z-ai/glm, moonshotai/kimi)
    is NOT a known-provider head must resolve to its OWNING provider via the registry,
    not silently fall through to the env default. Regression: grok/x-ai/z-ai/glm were
    unmapped and misrouted."""
    from modules.llm.llm_client_registry import AVAILABLE_MODELS

    # Pick a live glm slug owned by openrouter and a kimi slug owned by nvidia.
    glm = next((m for m in AVAILABLE_MODELS.get("openrouter", []) if "glm" in m.lower()), None)
    kimi = next((m for m in AVAILABLE_MODELS.get("nvidia", []) if "kimi" in m.lower()), None)
    assert glm, "expected an openrouter glm model in the registry"
    assert kimi, "expected an nvidia kimi model in the registry"
    assert map_model(glm) == ("openrouter", glm)
    assert map_model(kimi) == ("nvidia", kimi)
