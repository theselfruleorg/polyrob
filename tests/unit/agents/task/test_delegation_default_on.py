"""C-DELEG — ship delegation ON with conservative caps.

`delegate_task` was implemented but dark (SUB_AGENTS_ENABLED defaulted False). Per
the parity decision it ships ON by default, with the existing conservative caps
(depth=1, max 3 concurrent, 600s/900s timeouts) unchanged. Operators can still set
SUB_AGENTS_ENABLED=false to opt out.
"""
from core.config import BotConfig
from agents.task.constants import TimeoutConfig


def test_botconfig_sub_agents_enabled_defaults_true():
    assert BotConfig.model_fields["sub_agents_enabled"].default is True


def test_conservative_caps_unchanged():
    assert BotConfig.model_fields["max_sub_agent_depth"].default == 1
    assert BotConfig.model_fields["max_concurrent_sub_agents"].default == 3
    # The timeouts are TimeoutConfig constants (internal tuning, not env-bound).
    assert TimeoutConfig.SUB_AGENT_TIMEOUT == 600
    assert TimeoutConfig.PARALLEL_SUBTASKS_TIMEOUT == 900


def test_legacy_constant_reflects_on():
    assert TimeoutConfig.SUB_AGENTS_ENABLED is True


def test_env_fallback_default_is_true(monkeypatch):
    # When BotConfig is unavailable, the env fallback in get_sub_agents_enabled
    # should also default ON (parity with the config default).
    monkeypatch.setattr(TimeoutConfig, "_config", None)
    monkeypatch.setattr(TimeoutConfig, "_get_config", classmethod(lambda cls: None))
    monkeypatch.delenv("SUB_AGENTS_ENABLED", raising=False)
    assert TimeoutConfig.get_sub_agents_enabled() is True
