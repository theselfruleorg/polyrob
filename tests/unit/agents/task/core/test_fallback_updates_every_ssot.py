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

    def recalibrate_for_model(self, model):
        self.recalibrated_for = model

    def set_runtime_identity(self, model, provider):
        self.identity = (model, provider)


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
