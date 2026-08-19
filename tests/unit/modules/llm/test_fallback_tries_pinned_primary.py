"""``get_fallback_chat_model`` must try the (operator-pinned) PRIMARY client
before walking the generic ``FALLBACK_HIERARCHY``.

Live-prod evidence (2026-08-14, Rob #1): the box served on the operator-pinned
``zai-coding`` subscription seat (``DEFAULT_PROVIDER=zai-coding``), which is
deliberately ``fallback_eligible=False`` so the generic hierarchy never lists
it. A persisted session that requested the keyless ``openai`` provider walked
the hierarchy — anthropic/gemini keyless, openrouter 402 out-of-credits — and
died with "No fallback providers available" while a perfectly healthy pinned
primary client sat in ``self.clients``. The operator's explicit serving choice
beats the generic hierarchy; ``fallback_eligible`` governs only the hierarchy.
"""

import pytest

from core.config import BotConfig
from modules.llm.llm_manager import LLMManager


class _FakeClient:
    def __init__(self, model_type):
        self.model_type = model_type
        self.initialized = False

    async def initialize(self):
        self.initialized = True


def _build_manager(monkeypatch, *, primary=None, clients=None, llm_config=None):
    mgr = LLMManager(name="llm", config=BotConfig())
    mgr._initialized = True
    mgr._container = object()
    mgr.llm_config = llm_config or {}
    mgr.clients = clients or {}
    mgr.primary_client_name = primary

    async def _healthy(client):
        return True

    monkeypatch.setattr(mgr, "_test_client_health", _healthy)

    def _fake_create_llm_client(*, name, config, container, model_type=None):
        return _FakeClient(model_type=model_type)

    monkeypatch.setattr(
        "modules.llm.llm_manager.create_llm_client", _fake_create_llm_client
    )

    captured = {}

    def _fake_create_chat_model(*, provider, model, temperature, llm_client, **kwargs):
        captured["provider"] = provider
        captured["model"] = model
        stub = type("StubChatModel", (), {})()
        stub.provider = provider
        stub.model_name = model
        return stub

    monkeypatch.setattr(
        "modules.llm.llm_factory.create_chat_model", _fake_create_chat_model
    )
    mgr._test_captured = captured
    return mgr


@pytest.mark.asyncio
async def test_fallback_lands_on_pinned_primary_not_in_hierarchy(monkeypatch):
    """openai fails on a zai-coding-pinned box: the fallback must return the
    primary client's provider/model even though zai-coding is
    fallback_eligible=False (absent from FALLBACK_HIERARCHY)."""
    mgr = _build_manager(
        monkeypatch,
        primary="zai-coding_client",
        clients={"zai-coding_client": _FakeClient(model_type="glm-5")},
        llm_config={
            "openai": {},  # keyless -> _try_initialize_client short-circuits
            "zai-coding": {"api_key": "zai-" + "x" * 32},
        },
    )

    result = await mgr.get_fallback_chat_model(
        exclude_providers=["openai"], original_model="gpt-5", temperature=0.0
    )

    assert result is not None
    assert mgr._test_captured["provider"] == "zai-coding"
    assert mgr._test_captured["model"] == "glm-5"


@pytest.mark.asyncio
async def test_primary_named_fallback_client_resolves_real_provider(monkeypatch):
    """``_ensure_fallback_client`` registers 'openai_fallback_client', which can
    become the primary (first-available branch of ``_set_primary_client``).
    Stripping only the '_client' suffix yields the bogus provider
    'openai_fallback' — the provider name must match what the rest of the
    manager derives (``.replace('_client','').replace('_fallback','')``)."""
    mgr = _build_manager(
        monkeypatch,
        primary="openai_fallback_client",
        # No model_type on the client -> forces the get_default_model(provider)
        # path, where a bogus provider name silently mis-resolves the model.
        clients={"openai_fallback_client": _FakeClient(model_type=None)},
        llm_config={"openai": {"api_key": "sk-" + "x" * 44}},
    )

    result = await mgr.get_fallback_chat_model(
        exclude_providers=["anthropic"], original_model="claude-x", temperature=0.0
    )

    assert result is not None
    assert mgr._test_captured["provider"] == "openai"


@pytest.mark.asyncio
async def test_excluded_primary_is_skipped(monkeypatch):
    """The primary is NOT retried when it is the provider that just failed."""
    mgr = _build_manager(
        monkeypatch,
        primary="zai-coding_client",
        clients={"zai-coding_client": _FakeClient(model_type="glm-5")},
        llm_config={"zai-coding": {"api_key": "zai-" + "x" * 32}},
    )

    result = await mgr.get_fallback_chat_model(
        exclude_providers=["zai-coding"], original_model="glm-5", temperature=0.0
    )

    assert result is None
    assert "provider" not in mgr._test_captured
