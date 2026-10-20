"""Every run honors the operator's model pin (``DEFAULT_MODEL``).

Live prod 2026-10-12: DEFAULT_PROVIDER=openrouter + DEFAULT_MODEL=deepseek/... served
chat on DeepSeek, but cron/goal/planner runs filled the model via
``get_default_model`` and got the registry default (``z-ai/glm-5.2``).
"""
import pytest

from core.config_policy.retired_flags import RetiredFlagError
from core.runtime_config import (operator_model_for, operator_provider_pin,
                                 resolve_default_provider)
from modules.llm.llm_client_registry import DEFAULT_MODELS, get_default_model

PIN = {"DEFAULT_PROVIDER": "openrouter", "DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash"}


def _env(monkeypatch, **kv):
    for k in ("CHAT_PROVIDER", "CHAT_MODEL", "DEFAULT_PROVIDER", "DEFAULT_MODEL",
              "POLYROB_OPENROUTER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kv.items():
        monkeypatch.setenv(k, v)


def test_pinned_provider_gets_pinned_model():
    assert operator_model_for("openrouter", env=PIN) == "deepseek/deepseek-v4.1-flash"


def test_retired_chat_pin_alone_refuses():
    """CHAT_* is a refusing alias of DEFAULT_*: alone it refuses, never ignored."""
    with pytest.raises(RetiredFlagError, match="DEFAULT_MODEL"):
        operator_model_for("openrouter", env={"DEFAULT_PROVIDER": "openrouter",
                                              "CHAT_MODEL": "x-ai/grok-4.3"})
    with pytest.raises(RetiredFlagError, match="DEFAULT_PROVIDER"):
        operator_provider_pin({"CHAT_PROVIDER": "openrouter"})


def test_default_pin_wins_over_a_stale_chat_line():
    env = dict(PIN, CHAT_PROVIDER="anthropic", CHAT_MODEL="x-ai/grok-4.3")
    assert operator_model_for("openrouter", env=env) == "deepseek/deepseek-v4.1-flash"


def test_other_provider_never_inherits_the_pinned_model():
    # A job pinned elsewhere, or a credit-dead re-route, keeps its own default.
    assert operator_model_for("anthropic", env=PIN) is None


def test_no_pin_or_no_model_is_none():
    assert operator_model_for("openrouter", env={}) is None
    assert operator_model_for("openrouter", env={"DEFAULT_PROVIDER": "openrouter"}) is None
    assert operator_model_for(None, env=PIN) is None


def test_get_default_model_serves_the_pin_for_the_pinned_provider(monkeypatch):
    # The read cron, goals, the planner, the clients and `polyrob model list` share.
    _env(monkeypatch, **PIN)
    assert get_default_model("openrouter") == "deepseek/deepseek-v4.1-flash"
    assert get_default_model("anthropic") == DEFAULT_MODELS["anthropic"]


def test_retired_per_provider_override_is_not_read(monkeypatch):
    _env(monkeypatch, POLYROB_OPENROUTER_MODEL="x-ai/grok-4.3", **PIN)
    assert get_default_model("openrouter") == "deepseek/deepseek-v4.1-flash"
    _env(monkeypatch, POLYROB_OPENROUTER_MODEL="x-ai/grok-4.3")
    assert get_default_model("openrouter") == DEFAULT_MODELS["openrouter"]


def test_autonomous_default_resolution_carries_the_model(monkeypatch):
    _env(monkeypatch, **PIN)
    assert resolve_default_provider() == ("openrouter", "deepseek/deepseek-v4.1-flash")


def test_provider_pin_is_stripped(monkeypatch):
    _env(monkeypatch, CHAT_PROVIDER="  ", DEFAULT_PROVIDER=" openrouter ")
    assert operator_provider_pin() == "openrouter"
