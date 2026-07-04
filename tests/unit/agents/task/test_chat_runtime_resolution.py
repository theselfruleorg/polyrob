"""chat_once provider/model resolution (Phase 2) — delegates to the shared core
resolver, preserving the historical openai/gpt-5 default when an OpenAI key is
present, and falling through to the first keyed provider only when it isn't (the
latent only-one-key server-chat bug). Model is always filled to match the provider.
"""
from agents.task_agent_lite import _resolve_chat_runtime


def test_preserves_openai_default_when_openai_keyed():
    prov, model = _resolve_chat_runtime(env={"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y"})
    assert prov == "openai"
    assert model == "gpt-5"


def test_first_keyed_provider_when_openai_absent():
    # Only an Anthropic key: chat must NOT try gpt-5/openai (the bug) — it routes
    # to anthropic with anthropic's registry default model. The key value must
    # pass looks_like_real_key (placeholder/too-short values are rejected by the
    # usability oracle, which would fall through to the openai last-resort).
    prov, model = _resolve_chat_runtime(env={"ANTHROPIC_API_KEY": "sk-ant-" + "a" * 40})
    assert prov == "anthropic"
    assert model and "gpt-5" not in model  # provider-appropriate model, not the openai default


def test_chat_provider_env_pin_wins():
    prov, model = _resolve_chat_runtime(
        env={"CHAT_PROVIDER": "openrouter", "CHAT_MODEL": "z-ai/glm-5.2", "OPENAI_API_KEY": "x"}
    )
    assert prov == "openrouter"
    assert model == "z-ai/glm-5.2"


def test_default_provider_pin_without_model_fills_registry_default():
    prov, model = _resolve_chat_runtime(env={"DEFAULT_PROVIDER": "anthropic", "OPENAI_API_KEY": "x"})
    assert prov == "anthropic"
    assert model  # filled from the registry, not left as SessionRequest's gpt-5


def test_no_keys_falls_back_to_openai_gpt5():
    prov, model = _resolve_chat_runtime(env={})
    assert prov == "openai"
    assert model == "gpt-5"
