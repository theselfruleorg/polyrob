"""P0.6: llm_manager.get_available_models delegates to the ONE catalog while preserving its
List[Tuple[str,str]] shape + initialized-providers-only semantics. Behavioral safety net."""
import logging
import pytest

from modules.llm.llm_manager import LLMManager


def _mgr(clients):
    mgr = LLMManager.__new__(LLMManager)   # bypass __init__; set only what the method reads
    mgr._initialized = True
    mgr.clients = clients
    mgr.logger = logging.getLogger("test.llm_manager")
    return mgr


@pytest.mark.asyncio
async def test_returns_provider_model_tuples_for_initialized(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-" + "x" * 24)
    out = await _mgr({"openrouter_client": object()}).get_available_models()
    assert out, "an initialized provider with a usable key must yield models"
    assert all(isinstance(t, tuple) and len(t) == 2 for t in out)
    assert all(prov == "openrouter" for prov, _ in out)
    # the tuples name real registry models (not empty strings)
    assert all(model for _, model in out)


@pytest.mark.asyncio
async def test_uninitialized_provider_returns_empty():
    out = await _mgr({}).get_available_models(provider="openrouter")
    assert out == []


@pytest.mark.asyncio
async def test_no_initialized_clients_returns_empty():
    out = await _mgr({}).get_available_models()
    assert out == []


def test_available_models_registry_values_are_eager_lists():
    # guards the dead `callable(models_fn)` branch removed in task_http_api: AVAILABLE_MODELS
    # values are plain lists, never callables.
    from modules.llm.llm_client_registry import AVAILABLE_MODELS
    assert AVAILABLE_MODELS and all(isinstance(v, list) for v in AVAILABLE_MODELS.values())
