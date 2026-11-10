"""Proposal 106 cause 6: the one fallback ladder (R5, R8, D3, S5, L12).

The prices are the catalog's: flash is cheap, so z-ai/glm-5.2 is over 5x its input price.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.config import BotConfig
from core.exceptions import LLMAuthenticationError
from modules.llm.init_gate import note_call_error
from modules.llm.llm_client_registry import create_llm_client
from modules.llm.llm_manager import LLMManager
from modules.llm.model_types import model_family
from modules.llm.provider_spec import get_spec


class _Client:
    def __init__(self, model_type=None, name="client"):
        self.model_type = model_type
        self.name = name
        self._initialized = True

    async def initialize(self):
        pass


def _manager(monkeypatch, providers, primary=None):
    """A manager with one healthy, keyed client for each provider, and no paid call."""
    monkeypatch.delenv("LLM_FALLBACK_PROVIDERS", raising=False)
    monkeypatch.delenv("HEAVY_MODEL", raising=False)
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    mgr = LLMManager(name="llm", config=BotConfig())
    mgr._initialized = True
    mgr._container = object()
    mgr.llm_config = {p: {"api_key": "k"} for p in providers}
    mgr.clients = {f"{p}_client": _Client("shared", f"{p}_client") for p in providers}
    mgr.primary_client_name = primary

    def _create(*, name, config, container, model_type=None, isolated=False):
        assert isolated, "a fallback rung runs on an isolated client"
        return _Client(model_type, f"{name}_client")
    monkeypatch.setattr("modules.llm.llm_manager.create_llm_client", _create)
    monkeypatch.setattr(mgr, "_configure_client_token_limits", lambda c, m: None)
    monkeypatch.setattr("core.credit_sentinel.credit_sentinel_active",
                        lambda provider=None: False, raising=False)

    def _chat_model(*, provider, model, temperature, llm_client, **kwargs):
        return SimpleNamespace(provider=provider, model_name=model, _client=llm_client,
                               kwargs=kwargs)
    monkeypatch.setattr("modules.llm.llm_factory.create_chat_model", _chat_model)
    return mgr


def _walk(mgr, *exclude, original=None, money_run=False):
    return asyncio.run(mgr.get_fallback_chat_model(
        exclude_providers=list(exclude), original_model=original, money_run=money_run))


def test_native_flash_stays_in_its_family_on_openrouter(monkeypatch):
    mgr = _manager(monkeypatch, ["openrouter"])
    got = _walk(mgr, "deepseek", original="deepseek-flash")
    assert (got.provider, got.model_name) == ("openrouter", "deepseek/deepseek-v4.1-flash")


def test_native_flash_does_not_silently_move_to_a_pricier_family(monkeypatch):
    """A 429 on native DeepSeek with only a pricier model left: no move, the session waits."""
    mgr = _manager(monkeypatch, ["openrouter"])
    mgr.FALLBACK_HIERARCHY = [("openrouter_client", "z-ai/glm-5.2")]
    monkeypatch.setattr("modules.llm.llm_manager.AVAILABLE_MODELS", {"openrouter": ["z-ai/glm-5.2"]})
    assert _walk(mgr, "deepseek", original="deepseek-flash") is None


def test_pro_on_openrouter_reaches_the_same_model_natively(monkeypatch):
    mgr = _manager(monkeypatch, ["deepseek"])
    got = _walk(mgr, "openrouter", original="deepseek/deepseek-v4-pro")
    assert (got.provider, got.model_name) == ("deepseek", "deepseek-v4-pro")


def test_a_cheaper_family_may_serve(monkeypatch):
    mgr = _manager(monkeypatch, ["deepseek"])
    got = _walk(mgr, "openrouter", original="z-ai/glm-5.2")
    assert (got.provider, got.model_name) == ("deepseek", "deepseek-flash")


def test_an_unknown_price_does_not_move(monkeypatch):
    mgr = _manager(monkeypatch, ["deepseek"])
    mgr.FALLBACK_HIERARCHY = [("deepseek_client", "deepseek-newer-than-the-catalog")]
    monkeypatch.setattr("modules.llm.llm_manager.AVAILABLE_MODELS", {})
    assert _walk(mgr, "openrouter", original="z-ai/glm-5.2") is None


def test_a_current_model_with_no_row_can_fail_over(monkeypatch):
    mgr = _manager(monkeypatch, ["openrouter"])
    mgr.FALLBACK_HIERARCHY = [("openrouter_client", "z-ai/glm-5.2")]
    got = _walk(mgr, "acme", original="acme/no-row")
    assert (got.provider, got.model_name) == ("openrouter", "z-ai/glm-5.2")


def test_a_rung_above_the_guard_price_is_refused_when_the_current_price_is_unknown(monkeypatch):
    mgr = _manager(monkeypatch, ["openrouter"])
    mgr.FALLBACK_HIERARCHY = [("openrouter_client", "z-ai/glm-5.2")]
    monkeypatch.setattr("modules.llm.llm_manager._known_price",
                        lambda provider, model: (5.0, 31.0) if provider else None)
    assert _walk(mgr, "acme", original="acme/no-row") is None


def test_a_rung_with_no_price_is_refused_when_the_current_price_is_unknown(monkeypatch):
    mgr = _manager(monkeypatch, ["openrouter"])
    mgr.FALLBACK_HIERARCHY = [("openrouter_client", "acme/other-no-row")]
    assert _walk(mgr, "acme", original="acme/no-row") is None


def test_a_money_run_with_no_current_row_stays_on_a_money_class_model(monkeypatch):
    mgr = _manager(monkeypatch, ["openrouter"])
    mgr.FALLBACK_HIERARCHY = [("openrouter_client", "z-ai/glm-5.2")]
    assert _walk(mgr, "acme", original="acme/no-row", money_run=True) is None


def test_a_money_run_stays_on_a_money_class_model(monkeypatch):
    """Price alone lets pro drop to a cheaper family, so a money run has its own rule."""
    mgr = _manager(monkeypatch, ["deepseek"])
    assert _walk(mgr, "openrouter", original="z-ai/glm-5.2", money_run=True) is None
    got = _walk(mgr, "openrouter", original="deepseek/deepseek-v4-pro", money_run=True)
    assert got.model_name == "deepseek-v4-pro"
    monkeypatch.setenv("HEAVY_MODEL", "deepseek-flash")
    got = _walk(mgr, "openrouter", original="z-ai/glm-5.2", money_run=True)
    assert got.model_name == "deepseek-flash"


def test_a_rung_on_the_pinned_provider_serves_the_pin(monkeypatch):
    mgr = _manager(monkeypatch, ["openrouter"])
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setenv("DEFAULT_MODEL", "z-ai/glm-5.3-flash")
    monkeypatch.setattr("modules.llm.llm_manager.AVAILABLE_MODELS", {})
    mgr.FALLBACK_HIERARCHY = [("openrouter_client", "z-ai/glm-5.2")]
    got = _walk(mgr, "deepseek", original="deepseek-flash")
    assert got.model_name == "z-ai/glm-5.3-flash"


def test_a_stray_key_is_refused_with_the_flag_set(monkeypatch):
    mgr = _manager(monkeypatch, ["openai", "openrouter"])
    assert _walk(mgr, "deepseek", original="deepseek-flash").provider == "openrouter"
    mgr = _manager(monkeypatch, ["openai"])
    assert _walk(mgr, "anthropic", original="claude-sonnet-5-5").provider == "openai"
    monkeypatch.setenv("LLM_FALLBACK_PROVIDERS", "deepseek, openrouter")
    assert _walk(mgr, "anthropic", original="claude-sonnet-5-5") is None


def test_the_flag_also_filters_a_pinned_primary(monkeypatch):
    mgr = _manager(monkeypatch, ["deepseek", "zai-coding"], primary="zai-coding_client")
    mgr.clients["zai-coding_client"].model_type = "glm-5"
    monkeypatch.setenv("LLM_FALLBACK_PROVIDERS", "deepseek")
    got = _walk(mgr, "openrouter", original="z-ai/glm-5.2")
    assert got.provider == "deepseek"


def test_the_caller_list_is_not_changed(monkeypatch):
    """The session passes its own state list, and a build error here must not grow it."""
    mgr = _manager(monkeypatch, ["deepseek"])

    def _broken(**kwargs):
        raise RuntimeError("no adapter")
    monkeypatch.setattr("modules.llm.llm_factory.create_chat_model", _broken)
    failed = ["openrouter"]
    assert asyncio.run(mgr.get_fallback_chat_model(
        exclude_providers=failed, original_model="z-ai/glm-5.2")) is None
    assert failed == ["openrouter"]


def test_a_rung_whose_chat_model_fails_closes_its_isolated_client(monkeypatch):
    """The isolated client was built for this rung only: a failed build must close it."""
    mgr = _manager(monkeypatch, ["deepseek"])
    built = []

    class _Closing(_Client):
        closed = False

        async def cleanup(self):
            self.closed = True

    def _create(*, name, config, container, model_type=None, isolated=False):
        built.append(_Closing(model_type, f"{name}_client"))
        return built[-1]
    monkeypatch.setattr("modules.llm.llm_manager.create_llm_client", _create)

    def _broken(**kwargs):
        raise ValueError("Billed requests require an explicitly priced model")
    monkeypatch.setattr("modules.llm.llm_factory.create_chat_model", _broken)
    assert asyncio.run(mgr.get_fallback_chat_model(exclude_providers=[])) is None
    assert built and all(c.closed for c in built)


def test_a_rung_passes_no_model_ceiling(monkeypatch):
    """The factory resolves the capped per-request size; the manager passes nothing."""
    mgr = _manager(monkeypatch, ["deepseek"])
    assert "max_tokens" not in _walk(mgr, "openrouter", original="deepseek/deepseek-v4-pro").kwargs


def test_get_chat_model_passes_no_model_ceiling_and_keeps_an_explicit_size(monkeypatch):
    mgr = _manager(monkeypatch, ["openrouter"])
    mgr.clients["openrouter_client"] = type(
        "OpenRouterClient", (_Client,), {})("z-ai/glm-5.2", "openrouter_client")
    got = asyncio.run(mgr.get_chat_model("openrouter", "z-ai/glm-5.2"))
    assert "max_tokens" not in got.kwargs
    got = asyncio.run(mgr.get_chat_model("openrouter", "z-ai/glm-5.2", max_tokens=100))
    assert got.kwargs["max_tokens"] == 100


def test_an_error_on_an_isolated_client_marks_the_provider(monkeypatch):
    """S5: the health check reads the shared client, and the 401 came on an isolated one."""
    mgr = _manager(monkeypatch, ["openrouter", "deepseek"])
    isolated = asyncio.run(mgr._create_isolated_client("openrouter", "z-ai/glm-5.2"))
    shared = mgr.clients["openrouter_client"]
    shared._health_gate = mgr._init_gate
    assert mgr.provider_ready("openrouter")
    assert asyncio.run(mgr._test_client_health(shared))
    note_call_error(isolated, LLMAuthenticationError("401"))
    assert not mgr.provider_ready("openrouter")
    assert not asyncio.run(mgr._test_client_health(shared))
    assert mgr.provider_ready("deepseek")


def test_an_alias_reuses_the_shared_client(monkeypatch):
    """L12: deepseek-pro and deepseek-v4-pro are one model, so no second client."""
    from modules.llm.deepseek_client import DeepSeekClient
    mgr = _manager(monkeypatch, ["deepseek"])
    shared = object.__new__(DeepSeekClient)
    shared._model_type = "deepseek-v4-pro"
    mgr.clients["deepseek_client"] = shared

    async def _no_build(provider, model):
        raise AssertionError("no isolated client for an alias")
    monkeypatch.setattr(mgr, "_create_isolated_client", _no_build)
    got = asyncio.run(mgr.get_chat_model("deepseek", "deepseek-pro"))
    assert got._client is shared


def test_an_isolated_client_stays_out_of_the_container_and_does_not_validate():
    """Audit 18 and L12."""
    registered = {}
    container = SimpleNamespace(
        has_service=lambda name: name in registered,
        get_service=registered.get,
        register_service=lambda name, client, **kw: registered.__setitem__(name, client),
    )
    config = SimpleNamespace(get_llm_config=lambda: {"openrouter": {"api_key": "k"}})
    from unittest.mock import patch
    with patch("modules.llm.openrouter_client.OpenRouterClient.__init__", lambda self, config, name: None):
        client = create_llm_client("openrouter", config, container, "z-ai/glm-5.2", isolated=True)
    assert registered == {}
    assert client._skip_validate is True


def test_the_ladder_filter_reads_canonical_names(monkeypatch):
    from core.runtime_config import fallback_provider_allowlist as allowlist
    assert allowlist({}) is None
    assert allowlist({"LLM_FALLBACK_PROVIDERS": " , "}) is None
    assert allowlist({"LLM_FALLBACK_PROVIDERS": "OpenRouter, deepseek"}) == {"openrouter", "deepseek"}


@pytest.mark.parametrize("model,family", [
    ("deepseek-flash", "deepseek"), ("deepseek/deepseek-v4-pro", "deepseek"),
    ("z-ai/glm-5.2", "glm"), ("glm-5.3", "glm"), ("claude-sonnet-5-5", "claude"),
    ("qwen/qwen3-coder", "qwen"), ("no-such-vendor", None), (None, None),
])
def test_model_family(model, family):
    assert model_family(model) == family


def test_deepseek_spec_is_ranked_after_openrouter():
    assert get_spec("deepseek").fallback_rank == get_spec("openrouter").fallback_rank + 1
