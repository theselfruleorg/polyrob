"""The in-session LLM fallback must skip a provider the credit sentinel latched.

Prod 2026-08-24..28: OpenRouter was keyed but at $0. Its /models health check
answers 200, so the fallback ladder tried it for the real call and got 402 —
every time the funded primary (zai-coding) had a transient hiccup. Once the
cron/goal dispatch paths already preferred the funded seat, this fallback rung
was the remaining source of OpenRouter 402s. A sentinel-latched provider must
be skipped here too.
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


def _manager(monkeypatch, *, clients, hierarchy, dead):
    mgr = LLMManager(name="llm", config=BotConfig())
    mgr._initialized = True
    mgr._container = object()
    mgr.llm_config = {"openrouter": {"api_key": "or-" + "x" * 32},
                      "zai-coding": {"api_key": "zai-" + "x" * 32}}
    mgr.clients = clients
    mgr.primary_client_name = None
    mgr.FALLBACK_HIERARCHY = hierarchy

    async def _healthy(client):
        return True
    monkeypatch.setattr(mgr, "_test_client_health", _healthy)

    def _fake_create_llm_client(*, name, config, container, model_type=None):
        return _FakeClient(model_type=model_type)
    monkeypatch.setattr("modules.llm.llm_manager.create_llm_client",
                        _fake_create_llm_client)

    monkeypatch.setattr("core.credit_sentinel.credit_sentinel_active",
                        lambda provider=None: provider in dead, raising=False)

    captured = {}

    def _fake_create_chat_model(*, provider, model, temperature, llm_client, **kwargs):
        captured["provider"] = provider
        stub = type("StubChatModel", (), {})()
        stub.provider = provider
        return stub
    monkeypatch.setattr("modules.llm.llm_factory.create_chat_model",
                        _fake_create_chat_model)
    mgr._test_captured = captured
    return mgr


@pytest.mark.asyncio
async def test_credit_dead_provider_is_skipped_and_a_live_one_is_used(monkeypatch):
    mgr = _manager(
        monkeypatch,
        clients={"openrouter_client": _FakeClient("deepseek/deepseek-v3.2"),
                 "zai-coding_client": _FakeClient("glm-5")},
        hierarchy=[("openrouter_client", "deepseek/deepseek-v3.2"),
                   ("zai-coding_client", "glm-5")],
        dead={"openrouter"},
    )
    result = await mgr.get_fallback_chat_model(original_model="glm-5")
    assert result is not None
    assert mgr._test_captured["provider"] == "zai-coding"  # not the credit-dead openrouter


@pytest.mark.asyncio
async def test_no_live_provider_returns_none_rather_than_hitting_the_dead_one(monkeypatch):
    mgr = _manager(
        monkeypatch,
        clients={"openrouter_client": _FakeClient("deepseek/deepseek-v3.2")},
        hierarchy=[("openrouter_client", "deepseek/deepseek-v3.2")],
        dead={"openrouter"},
    )
    result = await mgr.get_fallback_chat_model(original_model="glm-5")
    assert result is None
    assert "provider" not in mgr._test_captured
