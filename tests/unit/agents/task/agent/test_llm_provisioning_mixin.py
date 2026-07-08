"""P9 pass-6 — LLMProvisioningMixin extracted from service.py."""
import logging
import types

from agents.task.agent.core.llm_provisioning import LLMProvisioningMixin


def test_agent_composes_llm_provisioning_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, LLMProvisioningMixin)
    for m in ("_supports_streaming", "_create_llm_from_config_async", "_create_llm_from_config",
              "set_token_limits", "_get_model_max_completion_tokens", "_reconcile_native_tools",
              "set_tool_calling_method"):
        assert getattr(Agent, m).__qualname__.startswith("LLMProvisioningMixin")


class _Host(LLMProvisioningMixin):
    def __init__(self, provider="openai", library="ChatOpenAI"):
        self.logger = logging.getLogger("provisioning-test")
        self.provider_name = provider
        self.chat_model_library = library


def test_supports_streaming():
    assert _Host("anthropic")._supports_streaming() is True
    assert _Host("someunknown")._supports_streaming() is False
    # Google models are tagged 'gemini' by detect_llm_provider — both that and
    # the legacy 'google' alias must stream (else Gemini silently never does).
    assert _Host("gemini")._supports_streaming() is True
    assert _Host("google")._supports_streaming() is True
    assert _Host("nvidia")._supports_streaming() is True


def test_set_tool_calling_method_auto_resolves_to_function_calling():
    assert _Host(library="ChatAnthropic").set_tool_calling_method("auto") == "function_calling"
    # explicit method passes through
    assert _Host().set_tool_calling_method("json_mode") == "json_mode"


# --- B5: _provision_aux_llm walks the resolve_aux_chain() candidate list ----------

class _ChainHost(LLMProvisioningMixin):
    """Stub agent whose _create_llm_from_config fails for the first N candidates."""

    def __init__(self, fail_count=1, provider="openrouter"):
        self.logger = logging.getLogger("provisioning-chain-test")
        self.provider_name = provider
        self._fail_count = fail_count
        self.calls = []

    def _create_llm_from_config(self, config, isolated=False):
        self.calls.append((config, isolated))
        if len(self.calls) <= self._fail_count:
            return None
        return types.SimpleNamespace(sentinel=True, config=config)


def test_provision_aux_llm_walks_chain_to_first_success(monkeypatch):
    monkeypatch.setenv("AUX_MODEL_JUDGE", "claude-haiku-4-5")
    monkeypatch.setenv("AUX_PROVIDER_JUDGE", "anthropic")
    monkeypatch.setenv("AUX_FALLBACK_JUDGE", "openai/gpt-5-mini")
    host = _ChainHost(fail_count=1)
    result = host._provision_aux_llm("judge")
    assert result is not None and result.sentinel is True
    assert result.config == {"model": "gpt-5-mini", "provider": "openai"}
    assert len(host.calls) == 2
    assert host.calls[0][0] == {"model": "claude-haiku-4-5", "provider": "anthropic"}
    assert all(isolated is True for _, isolated in host.calls)


def test_provision_aux_llm_returns_none_when_all_candidates_fail(monkeypatch):
    # Hermeticity: ambient AUX_* must not change the candidate chain length/shape.
    for _v in ("AUX_PROVIDER_JUDGE", "AUX_PROVIDER", "AUX_AUTO", "AUX_MODEL"):
        monkeypatch.delenv(_v, raising=False)
    monkeypatch.setenv("AUX_MODEL_JUDGE", "claude-haiku-4-5")
    monkeypatch.setenv("AUX_FALLBACK_JUDGE", "openai/gpt-5-mini")
    host = _ChainHost(fail_count=99)
    assert host._provision_aux_llm("judge") is None
    assert len(host.calls) == 2


def test_provision_aux_llm_none_when_chain_empty(monkeypatch):
    monkeypatch.delenv("AUX_MODEL_JUDGE", raising=False)
    monkeypatch.delenv("AUX_AUTO", raising=False)
    host = _ChainHost()
    assert host._provision_aux_llm("judge") is None
    assert host.calls == []


def test_provision_aux_llm_no_trying_next_on_final_candidate(monkeypatch, caplog):
    # Log accuracy: the last (or only) candidate's failure must NOT say "trying
    # next candidate" — only the final "All aux candidates ... failed" summary.
    # Hermeticity: ambient AUX_* must not add candidates (which would reintroduce
    # a "trying next candidate" line).
    for _v in ("AUX_PROVIDER_JUDGE", "AUX_PROVIDER", "AUX_AUTO", "AUX_MODEL"):
        monkeypatch.delenv(_v, raising=False)
    monkeypatch.setenv("AUX_MODEL_JUDGE", "claude-haiku-4-5")
    monkeypatch.delenv("AUX_FALLBACK_JUDGE", raising=False)
    host = _ChainHost(fail_count=99)
    with caplog.at_level(logging.WARNING, logger="provisioning-chain-test"):
        assert host._provision_aux_llm("judge") is None
    assert "trying next candidate" not in caplog.text
    assert "All aux candidates for 'judge' failed" in caplog.text


# --- P2-9: async provisioning awaits the client build (no loop-blocking) -----------

class _AsyncChainHost(LLMProvisioningMixin):
    def __init__(self, provider="anthropic"):
        self.logger = logging.getLogger("provisioning-async-test")
        self.provider_name = provider
        self.calls = []

    async def _create_llm_from_config_async(self, config, isolated=False):
        self.calls.append((config, isolated))
        return types.SimpleNamespace(sentinel=True, config=config)


def test_provision_aux_llm_async_builds_isolated(monkeypatch):
    import asyncio
    monkeypatch.setenv("AUX_MODEL_JUDGE", "claude-haiku-4-5")
    monkeypatch.setenv("AUX_PROVIDER_JUDGE", "anthropic")
    host = _AsyncChainHost()
    result = asyncio.run(host._provision_aux_llm_async("judge"))
    assert result is not None and result.sentinel is True
    assert host.calls and host.calls[0][1] is True  # isolated=True


def test_provision_aux_llm_async_none_when_no_chain(monkeypatch):
    import asyncio
    for k in ("AUX_MODEL_JUDGE", "AUX_PROVIDER_JUDGE", "AUX_FALLBACK_JUDGE", "AUX_AUTO"):
        monkeypatch.delenv(k, raising=False)
    host = _AsyncChainHost()
    assert asyncio.run(host._provision_aux_llm_async("judge")) is None
