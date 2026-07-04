"""B14 — get_chat_model must not mutate the shared per-provider client to a
different model (cross-session bleed). When the requested model differs from the
shared client's model_type, it builds an ISOLATED client instead.
"""
import logging

import pytest

from modules.llm.llm_manager import LLMManager


class _FakeOpenrouterClient:
    def __init__(self, model_type):
        self.model_type = model_type


def _manager():
    m = object.__new__(LLMManager)
    m._initialized = True
    m.logger = logging.getLogger("llm-mgr-test")
    return m


@pytest.mark.asyncio
async def test_isolated_client_built_when_model_differs(monkeypatch):
    m = _manager()
    shared = _FakeOpenrouterClient("x-ai/grok-4.3")           # shared client's model
    isolated = _FakeOpenrouterClient("z-ai/glm-4.6")          # per-request model
    built = {"isolated_for": None}

    async def fake_get_client(name):
        return shared

    async def fake_create_isolated(provider, model):
        built["isolated_for"] = (provider, model)
        return isolated

    captured = {}

    def fake_create_chat_model(*, provider, model, temperature, llm_client, **kw):
        captured["client"] = llm_client
        return object()  # stand-in adapter

    monkeypatch.setattr(m, "get_client", fake_get_client)
    monkeypatch.setattr(m, "_create_isolated_client", fake_create_isolated)
    monkeypatch.setattr("modules.llm.llm_manager.create_chat_model", fake_create_chat_model, raising=False)
    # skip the client-type sanity check (fake class name lacks the provider substring)
    monkeypatch.setattr("modules.llm.llm_factory.create_chat_model", fake_create_chat_model, raising=False)

    await m.get_chat_model(provider="openrouter", model="z-ai/glm-4.6")

    assert built["isolated_for"] == ("openrouter", "z-ai/glm-4.6")
    assert captured["client"] is isolated  # NOT the shared client


@pytest.mark.asyncio
async def test_shared_client_reused_when_model_matches(monkeypatch):
    m = _manager()
    shared = _FakeOpenrouterClient("z-ai/glm-4.6")
    called = {"isolated": False}

    async def fake_get_client(name):
        return shared

    async def fake_create_isolated(provider, model):
        called["isolated"] = True
        return _FakeOpenrouterClient(model)

    captured = {}

    def fake_create_chat_model(*, provider, model, temperature, llm_client, **kw):
        captured["client"] = llm_client
        return object()

    monkeypatch.setattr(m, "get_client", fake_get_client)
    monkeypatch.setattr(m, "_create_isolated_client", fake_create_isolated)
    monkeypatch.setattr("modules.llm.llm_factory.create_chat_model", fake_create_chat_model, raising=False)

    await m.get_chat_model(provider="openrouter", model="z-ai/glm-4.6")

    assert called["isolated"] is False    # model matches -> no isolation
    assert captured["client"] is shared
