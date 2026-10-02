"""WS-K4: MEMORY_CONSOLIDATE defaults OFF (the recall store holds narration, not lessons)."""

from core.config_policy.autonomy_config import AutonomyConfig


def test_memory_consolidate_default_off(monkeypatch):
    monkeypatch.delenv("MEMORY_CONSOLIDATE", raising=False)
    assert AutonomyConfig.memory_consolidate() is False


def test_memory_consolidate_opt_in(monkeypatch):
    monkeypatch.setenv("MEMORY_CONSOLIDATE", "true")
    assert AutonomyConfig.memory_consolidate() is True
