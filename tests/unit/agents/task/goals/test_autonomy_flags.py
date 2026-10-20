from core.config_policy import AutonomyConfig


def test_defaults(monkeypatch):
    for var in (
        "GOAL_DAILY_QUOTA",
        "GOAL_SELF_WAKE_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    assert AutonomyConfig.goal_daily_quota() == 6
    assert AutonomyConfig.goal_self_wake_enabled() is False
    assert AutonomyConfig.goal_dedup_threshold() == 0.6


def test_overrides(monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "3")
    monkeypatch.setenv("GOAL_SELF_WAKE_ENABLED", "true")
    assert AutonomyConfig.goal_daily_quota() == 3
    assert AutonomyConfig.goal_self_wake_enabled() is True
