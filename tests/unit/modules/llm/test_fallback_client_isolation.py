"""Fallback model must run on an ISOLATED client — no shared-client clobber.

`get_fallback_chat_model` reused the SHARED cached per-provider client and passed
it to `create_chat_model`, which mutates `client.model_type` in place. On a
same-provider failover (e.g. openai gpt-5 -> openai gpt-5-mini) that clobbered the
main agent's live client. The fix builds the fallback on a fresh, non-cached client
(mirrors the compaction-aux `_create_isolated_client` path).
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


@pytest.mark.asyncio
async def test_fallback_builds_on_isolated_client_not_shared(monkeypatch):
    mgr = LLMManager(name="llm", config=BotConfig())
    mgr._initialized = True
    mgr._container = object()  # avoid lazy DependencyContainer creation in test

    # Main agent's live, shared cached client (same provider as the fallback).
    main_client = _FakeClient(model_type="gpt-5")
    mgr.clients = {"openai_client": main_client}
    mgr.llm_config = {"openai": {"api_key": "k", "model": "gpt-5"}}

    # Force a same-provider, different-model failover.
    mgr.FALLBACK_HIERARCHY = [("openai_client", "gpt-5-mini")]

    # Shared client passes the cheap health check.
    async def _healthy(client):
        return True
    monkeypatch.setattr(mgr, "_test_client_health", _healthy)

    # The isolated client built by _create_isolated_client.
    iso_client = _FakeClient(model_type="placeholder")
    monkeypatch.setattr(
        "modules.llm.llm_manager.create_llm_client",
        lambda **kw: iso_client,
    )
    monkeypatch.setattr(mgr, "_configure_client_token_limits", lambda c, m: None)

    captured = {}

    def _fake_create_chat_model(*, provider, model, temperature, llm_client, **kwargs):
        # Reproduce the in-place model_type mutation that adapters perform.
        llm_client.model_type = model
        captured["client"] = llm_client
        captured["model"] = model
        return object()

    monkeypatch.setattr(
        "modules.llm.llm_factory.create_chat_model",
        _fake_create_chat_model,
    )

    result = await mgr.get_fallback_chat_model(
        exclude_providers=[], original_model="gpt-5"
    )

    assert result is not None
    # The factory was handed the ISOLATED client, not the shared one.
    assert captured["client"] is iso_client
    assert captured["client"] is not main_client
    assert captured["model"] == "gpt-5-mini"
    # The isolated client absorbed the mutation...
    assert iso_client.model_type == "gpt-5-mini"
    # ...and the shared main client's model_type is UNCHANGED.
    assert main_client.model_type == "gpt-5"
