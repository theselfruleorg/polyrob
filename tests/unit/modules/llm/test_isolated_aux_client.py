"""Isolated aux client — fixes the same-provider compaction-aux clobber.

Every adapter sets `client.model_type = self.model_name` at construction,
and generation reads `client.model_type`. Building the compaction aux on the SHARED
cached per-provider client therefore clobbers the main agent's model. The fix:
`_create_isolated_client` builds a FRESH, non-cached client for the aux so the main
agent's shared client is never mutated.
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
async def test_isolated_client_is_fresh_and_does_not_touch_cache(monkeypatch):
    mgr = LLMManager(name="llm", config=BotConfig())
    mgr._initialized = True
    mgr._container = object()  # avoid lazy DependencyContainer creation in test
    main_client = _FakeClient(model_type="x-ai/grok-4.3")
    mgr.clients = {"openrouter_client": main_client}
    mgr.llm_config = {"openrouter": {"api_key": "k", "model": "x-ai/grok-4.3"}}

    aux_client = _FakeClient(model_type="placeholder")
    # create_llm_client is imported into llm_manager's namespace at module load.
    monkeypatch.setattr(
        "modules.llm.llm_manager.create_llm_client",
        lambda **kw: aux_client,
    )
    monkeypatch.setattr(mgr, "_configure_client_token_limits", lambda c, m: None)

    result = await mgr._create_isolated_client("openrouter", "gemini-2.5-flash")

    # Returned the fresh aux client, initialized.
    assert result is aux_client
    assert aux_client.initialized is True
    # The shared cached client was NOT replaced and its model_type is untouched.
    assert mgr.clients["openrouter_client"] is main_client
    assert main_client.model_type == "x-ai/grok-4.3"


@pytest.mark.asyncio
async def test_isolated_client_returns_none_without_api_key(monkeypatch):
    mgr = LLMManager(name="llm", config=BotConfig())
    mgr._initialized = True
    mgr.clients = {}
    mgr.llm_config = {"openrouter": {}}  # no api_key

    result = await mgr._create_isolated_client("openrouter", "gemini-2.5-flash")
    assert result is None
