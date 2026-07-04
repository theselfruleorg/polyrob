"""P0.7 Part 2 evidence: proves the api/task_http_api.py::create_session static
``model="gpt-5"``/``provider="openai"`` request defaults do NOT get stuck on
openai when only an OpenRouter key is configured.

Context: ``api/models.py``'s ``SessionCreateRequest`` (the class the original P0.7
plan pointed at) turned out to be dead code — ``create_session`` takes a raw
``Dict[str, Any]`` body and never instantiates it. The literal that actually
matters lives at ``api/task_http_api.py:1059``
(``session_config.llm.model = request_body.get("model", "gpt-5")``), which flows
through ``SessionRequest`` into session metadata and is read back by
``TaskAgent._get_llm_for_request`` (``agents/task_agent_lite.py``) when the
session actually runs.

This test exercises ``_get_llm_for_request`` exactly as ``run_session`` calls it,
against an ``LLMManager`` configured with an OpenRouter key but NO OpenAI key —
mirroring an OpenRouter-only deployment. If the static "openai"/"gpt-5" request
defaults were passed straight through to the provider, this would raise. Instead
``LLMManager.get_chat_model`` raises ``ValueError`` for the unavailable "openai"
provider, ``_get_llm_for_request`` catches it, and
``LLMManager.get_fallback_chat_model`` walks ``FALLBACK_HIERARCHY`` (skipping
anthropic/gemini for lack of keys) onto ``openrouter_client`` — using the
registry's OWN default model for that provider (``get_default_model('openrouter')``
via ``FALLBACK_HIERARCHY``), never the stale "gpt-5" literal. This is the
downstream re-resolution the P0.7 brief asked to check for before touching
``api/models.py``.
"""

import pytest

from core.config import BotConfig
from modules.llm.llm_client_registry import get_default_model
from modules.llm.llm_manager import LLMManager
from agents.task_agent_lite import TaskAgent


class _FakeClient:
    def __init__(self, model_type):
        self.model_type = model_type
        self.initialized = False

    async def initialize(self):
        self.initialized = True


class _FakeContainer:
    """Just enough of DependencyContainer's surface for get_service('llm')."""

    def __init__(self, llm_manager):
        self._llm_manager = llm_manager

    def get_service(self, name):
        if name == "llm":
            return self._llm_manager
        return None


def _build_openrouter_only_manager(monkeypatch) -> LLMManager:
    """An LLMManager wired exactly like a deployment with only
    OPENROUTER_API_KEY set: 'openai'/'anthropic'/'gemini' all have no key."""
    mgr = LLMManager(name="llm", config=BotConfig())
    mgr._initialized = True
    mgr._container = object()  # never dereferenced (see api-key short-circuit below)

    # No 'model' override for openrouter -> _create_isolated_client resolves via
    # get_default_model('openrouter'), same as the FALLBACK_HIERARCHY tuple.
    mgr.llm_config = {
        "openai": {},  # no api_key -> _try_initialize_client short-circuits to
                        # None BEFORE ever touching self.container (no crash risk)
        "openrouter": {"api_key": "sk-or-" + "x" * 24},
        # anthropic/gemini deliberately absent -> .get(provider, {}) == {} -> same
        # clean no-api_key short-circuit as openai.
    }
    # openrouter pre-seeded so get_fallback_chat_model's hierarchy walk finds it
    # via `self.clients` directly (skips _try_initialize_client's container touch,
    # matching this repo's existing LLMManager test convention).
    mgr.clients = {"openrouter_client": _FakeClient(model_type="placeholder")}

    async def _healthy(client):
        return True

    monkeypatch.setattr(mgr, "_test_client_health", _healthy)

    captured_isolated = {}

    def _fake_create_llm_client(*, name, config, container, model_type=None):
        client = _FakeClient(model_type=model_type)
        captured_isolated[name] = client
        return client

    monkeypatch.setattr(
        "modules.llm.llm_manager.create_llm_client", _fake_create_llm_client
    )

    captured_chat_model = {}

    def _fake_create_chat_model(*, provider, model, temperature, llm_client, **kwargs):
        llm_client.model_type = model
        captured_chat_model["provider"] = provider
        captured_chat_model["model"] = model
        stub = type("StubChatModel", (), {})()
        stub.provider = provider
        stub.model_name = model
        return stub

    monkeypatch.setattr(
        "modules.llm.llm_factory.create_chat_model", _fake_create_chat_model
    )

    mgr._test_captured_chat_model = captured_chat_model  # stash for assertions
    return mgr


@pytest.mark.asyncio
async def test_llm_manager_reresolves_to_openrouter_when_openai_unavailable(monkeypatch):
    """Direct proof at the LLMManager layer: requesting openai/gpt-5 with no
    OpenAI key raises, and the fallback lands on openrouter with the REGISTRY
    default model, never the stale/unavailable 'gpt-5'."""
    mgr = _build_openrouter_only_manager(monkeypatch)

    with pytest.raises(ValueError):
        await mgr.get_chat_model(provider="openai", model="gpt-5", temperature=0.0)

    result = await mgr.get_fallback_chat_model(
        exclude_providers=["openai"], original_model="gpt-5", temperature=0.0
    )

    assert result is not None
    assert mgr._test_captured_chat_model["provider"] == "openrouter"
    assert mgr._test_captured_chat_model["model"] == get_default_model("openrouter")
    assert mgr._test_captured_chat_model["model"] != "gpt-5"


@pytest.mark.asyncio
async def test_get_llm_for_request_reresolves_session_defaults_to_openrouter(monkeypatch):
    """End-to-end proof at the ACTUAL consumer: TaskAgent._get_llm_for_request is
    exactly what run_session calls with the dict read back from session metadata
    (agents/task_agent_lite.py ~:843/:853). Feed it the literal request shape that
    api/task_http_api.py:1059 produces when a caller omits model/provider
    ({'provider': 'openai', 'model': 'gpt-5', ...}) against an OpenRouter-only
    LLMManager, and confirm it resolves to openrouter — not a crash, not gpt-5."""
    mgr = _build_openrouter_only_manager(monkeypatch)

    # Bypass TaskAgent.__init__ (heavy container/config wiring) — the method under
    # test only reads self.container.
    agent = object.__new__(TaskAgent)
    agent.container = _FakeContainer(mgr)

    request = {"provider": "openai", "model": "gpt-5", "temperature": 0.0}
    llm = await agent._get_llm_for_request(request)

    assert llm is not None
    assert mgr._test_captured_chat_model["provider"] == "openrouter"
    assert mgr._test_captured_chat_model["model"] == get_default_model("openrouter")
    assert mgr._test_captured_chat_model["model"] != "gpt-5"
