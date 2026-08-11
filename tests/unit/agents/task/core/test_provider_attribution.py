"""Error attribution names the SESSION's provider, not a model-name guess
(UX assessment 2026-08-07, Q12).

A `-p zai-coding` failure used to report "provider openrouter failed" +
"from AnthropicCompatClient" — two wrong names, never the one the user typed:
`current_provider` was inferred from the model name, and the adapter labeled
errors with the client class name.
"""
import textwrap
from types import SimpleNamespace

import pytest


@pytest.fixture
def zai_provider(tmp_path, monkeypatch):
    path = tmp_path / "providers.yaml"
    path.write_text(textwrap.dedent("""\
        providers:
          zai-coding:
            base_url: https://api.z.ai/api/anthropic
            auth_type: api_key
            env_key: ZAI_API_KEY
            transport: anthropic_messages
            models: [glm-5]
            default_model: glm-5
    """))
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
    monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
    from modules.llm.provider_spec import reset_provider_registry_cache
    reset_provider_registry_cache()
    yield
    reset_provider_registry_cache()


def _mixin_host(llm):
    from agents.task.agent.core.model_introspection import ModelIntrospectionMixin

    class Host(ModelIntrospectionMixin):
        def __init__(self):
            self.llm = llm

    return Host()


def test_current_provider_prefers_live_client_spec(zai_provider):
    client = SimpleNamespace(
        _spec=SimpleNamespace(name="zai-coding"), name="zai-coding_client"
    )
    host = _mixin_host(SimpleNamespace(_client=client))
    assert host._current_llm_provider("glm-5") == "zai-coding"


def test_current_provider_falls_back_to_model_inference():
    # a non-provider service name (openai_fallback_client) must not be trusted;
    # model inference resolves gpt-5 → openai
    client = SimpleNamespace(_spec=None, name="openai_fallback_client")
    host = _mixin_host(SimpleNamespace(_client=client))
    assert host._current_llm_provider("gpt-5") == "openai"


def test_current_provider_no_llm_uses_model_inference():
    host = _mixin_host(None)
    assert host._current_llm_provider("gpt-5") == "openai"


def test_adapter_error_label_names_provider(zai_provider):
    """The adapter's translate prefix must carry the provider, never the
    client class name."""
    from modules.llm.adapters import _client_provider_label

    compat = SimpleNamespace(_spec=SimpleNamespace(name="zai-coding"))
    assert _client_provider_label(compat) == "zai-coding"

    class AnthropicClientStub:
        _PROVIDER_LABEL = "Anthropic"
    assert _client_provider_label(AnthropicClientStub()) == "Anthropic"

    class Bare:
        pass
    assert _client_provider_label(Bare()) == "Bare"
