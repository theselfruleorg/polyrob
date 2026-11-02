"""Automatic provider fallback must update every model/provider SSOT, not two of them.

`swap_model` (the deliberate hot-swap) documents the full SSOT list and updates it.
The AUTOMATIC fallback in `llm_runner.get_next_action` set only `self.llm` and
`self.model_name`, and the switch is permanent for the rest of the session — so
afterwards:

  * `self.llm_provider` still named the provider that just FAILED, and the billing
    path reads exactly that (`next_action_internal.py`: `_native_provider =
    getattr(self, 'llm_provider', 'unknown')`), so every subsequent completion was
    recorded against the wrong provider;
  * `message_manager.llm` still pointed at the dead client, and compaction reads it
    (`compactor.py`: `getattr(self, "aux_llm", None) or self.llm`), so context
    compaction was attempted against the provider that had just rate-limited/failed.

Both paths now go through the shared `adopt_active_llm`.
"""
import pytest

from agents.task.agent.core.model_swap import ModelSwapMixin


class _MM:
    def __init__(self):
        self.llm = None
        self.model_name = None
        self.provider_name = None
        self.recalibrated_for = None
        self.identity = None
        self.identity_calls = []
        self.prompt_rebuilds = 0
        self.anchor_resets = []

    def recalibrate_for_model(self, model):
        self.recalibrated_for = model

    def set_runtime_identity(self, model, provider, switched_from=None, cause=None,
                             reason=None, at=None):
        self.identity = (model, provider)
        self.identity_calls.append((model, provider, switched_from, cause, reason))

    def rebuild_system_prompt(self, use_native_tools=None):
        self.prompt_rebuilds += 1
        return True

    def reset_usage_anchor(self, reason=""):
        self.anchor_resets.append(reason)


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Agent(ModelSwapMixin):
    """Minimal stand-in exposing the same SSOT surface the real Agent does."""

    def __init__(self):
        self.llm = "openai-client"
        self.llm_provider = "openai"
        self.chat_model_library = "OpenAIClient"
        self.message_manager = _MM()
        self.logger = _Logger()
        self.reconciled_for = None
        self._model_name = "gpt-5"
        self._provider_name = "openai"

    # Mirror the real Agent's property delegation to the MessageManager SSOT.
    @property
    def model_name(self):
        return self._model_name

    @model_name.setter
    def model_name(self, value):
        self._model_name = value
        self.message_manager.model_name = value

    @property
    def provider_name(self):
        return self._provider_name

    @provider_name.setter
    def provider_name(self, value):
        self._provider_name = value
        self.message_manager.provider_name = value

    def _reconcile_native_tools(self, provider):
        self.reconciled_for = provider


def test_adopt_updates_every_ssot_swap_model_documents():
    a = _Agent()
    a.adopt_active_llm("anthropic-client", "claude-x", "anthropic")

    assert a.llm == "anthropic-client"
    # The one the billing path reads — the whole point of the fix.
    assert a.llm_provider == "anthropic"
    assert a.chat_model_library == "str"
    assert a.model_name == "claude-x"
    assert a.provider_name == "anthropic"
    # The one compaction reads.
    assert a.message_manager.llm == "anthropic-client"
    assert a.message_manager.model_name == "claude-x"
    assert a.message_manager.provider_name == "anthropic"
    assert a.message_manager.recalibrated_for == "claude-x"
    # 095 W1a: the identity line is the caller's ONE refresh, not adopt's.
    assert a.message_manager.identity is None
    a._refresh_after_model_change("fallback", "gpt-5", reason="LLMRateLimitError")
    assert a.message_manager.identity == ("claude-x", "anthropic")
    assert a.reconciled_for == "anthropic"


def test_billing_provider_never_keeps_naming_the_failed_provider():
    """The precise regression: after falling back off openai, nothing may still
    report 'openai' as the active provider."""
    a = _Agent()
    assert a.llm_provider == "openai"
    a.adopt_active_llm("anthropic-client", "claude-x", "anthropic")
    assert a.llm_provider != "openai"
    assert a.message_manager.llm != "openai-client"


def test_adopt_is_reversible_for_the_failed_fallback_path():
    """The fallback handlers restore the original on failure; that restore must be
    symmetric with the switch or it leaves a half-reverted agent."""
    a = _Agent()
    original = (a.llm, a.model_name, a.llm_provider)

    a.adopt_active_llm("anthropic-client", "claude-x", "anthropic")
    a.adopt_active_llm(*original[:2], original[2])

    assert (a.llm, a.model_name, a.llm_provider) == original
    assert a.message_manager.llm == original[0]
    assert a.message_manager.provider_name == "openai"


def test_recalibration_failure_does_not_abort_the_switch():
    """Best-effort steps must not leave the agent on a half-applied model."""
    a = _Agent()

    def _boom(model):
        raise RuntimeError("registry miss")

    a.message_manager.recalibrate_for_model = _boom
    a.adopt_active_llm("anthropic-client", "claude-x", "anthropic")
    assert a.llm_provider == "anthropic"
    assert a.message_manager.llm == "anthropic-client"


def test_both_fallback_handlers_use_the_shared_adopter():
    """Structural: guards against a future edit re-introducing a two-attribute switch
    in either handler."""
    import inspect

    from agents.task.agent.core import llm_runner

    src = inspect.getsource(llm_runner)
    assert src.count("self.adopt_active_llm(") >= 4, (
        "expected both fallback handlers to adopt on success AND restore on failure "
        "through the shared adopter"
    )
    assert "self.llm = fallback_llm" not in src, (
        "a fallback handler is setting self.llm directly again — that skips "
        "llm_provider and message_manager.llm"
    )


# --- 095 W1a: one refresh for every model change ------------------------------------ #

def test_fallback_rebuilds_family_note():
    """X-2: a fallback rebuilds the system prompt (model-family note, JSON action
    list) and drops the usage anchor, like the owner's swap does."""
    a = _Agent()
    a.adopt_active_llm("anthropic-client", "claude-x", "anthropic")
    a._refresh_after_model_change("fallback", "gpt-5", reason="LLMConnectionError")
    assert a.message_manager.prompt_rebuilds == 1
    assert a.message_manager.anchor_resets == ["model change (fallback)"]


def test_fallback_never_says_owner_switched():
    """SK-14/HI-12: the identity line after a fallback names the failover and its
    reason — never "The owner switched you"."""
    from agents.task.agent.message_manager.service import MessageManager
    from agents.task.agent.prompts import SystemPrompt
    mm = MessageManager(llm=None, task="t", action_descriptions="x",
                        system_prompt_class=SystemPrompt, max_input_tokens=100000)
    mm.set_runtime_identity("claude-x", "anthropic", switched_from="gpt-5",
                            cause="fallback", reason="LLMRateLimitError")
    text = mm._runtime_identity_message.content
    assert "The owner switched" not in text
    assert 'failed over from "gpt-5"' in text and "LLMRateLimitError" in text
    mm.set_runtime_identity("m2", "openrouter", switched_from="m1", cause="api")
    assert "The owner switched" not in mm._runtime_identity_message.content
    mm.set_runtime_identity("m2", "openrouter", switched_from="m1", cause="owner")
    assert 'The owner switched you to it from "m1"' in mm._runtime_identity_message.content


def test_fallback_callers_share_the_refresh():
    """Every fallback site calls the shared refresh with cause=fallback, and each
    restore after a failed fallback refreshes too."""
    import inspect
    from agents.task.agent.core import error_recovery, llm_runner
    from agents.task.session import execution
    runner = inspect.getsource(llm_runner)
    assert runner.count('_refresh_after_model_change("fallback"') == 1
    assert runner.count('_refresh_after_model_change(\n\t\t\t\t\t\t"fallback"') == 1
    assert runner.count('_refresh_after_model_change("restore")') == 2
    assert "_get_provider_from_model(fallback_model)" not in runner
    assert '"fallback", original_model, reason=original_error_type' in inspect.getsource(error_recovery)
    assert 'agent._refresh_after_model_change(' in inspect.getsource(execution)


def test_one_identity_write_per_swap():
    """X-11: swap_model writes the identity line ONCE (adopt no longer writes it)."""
    import asyncio
    a = _Agent()

    async def build(cfg, isolated=False):
        return "anthropic-client"

    a._create_llm_from_config_async = build
    a._provision_compaction_llm = lambda: None
    r = asyncio.run(a.swap_model("anthropic", "claude-x", cause="owner"))
    assert r["ok"]
    assert a.message_manager.identity_calls == [
        ("claude-x", "anthropic", "gpt-5", "owner", None)]


def test_vision_follows_swap_and_fallback():
    """SK-1: vision is re-resolved from the CONFIGURED value on every model change:
    a blind model clears it, a vision model restores it, and the manager's route
    note input follows."""
    a = _Agent()
    a._configured_use_vision = True
    a.use_vision = True
    a.message_manager._include_vision = True
    a._extract_model_name = lambda llm: llm
    a._check_vision_support = lambda name: name != "blind-client"
    a.adopt_active_llm("blind-client", "glm-5", "zai")
    assert a.use_vision is False and a.message_manager._include_vision is False
    a.adopt_active_llm("seeing-client", "gpt-5", "openai")
    assert a.use_vision is True and a.message_manager._include_vision is True
