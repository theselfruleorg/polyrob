from agents.task.constants import AutonomyConfig


def test_defaults(monkeypatch):
    for var in (
        "GOAL_PLANNER_ENABLED",
        "GOAL_PLANNER_MIN_READY",
        "GOAL_PLANNER_COOLDOWN_SEC",
        "GOAL_PLANNER_HISTORY_N",
        "GOAL_DAILY_QUOTA",
        "GOAL_SELF_WAKE_ENABLED",
        "GOAL_DEDUP_THRESHOLD",
    ):
        monkeypatch.delenv(var, raising=False)
    assert AutonomyConfig.goal_planner_enabled() is False
    assert AutonomyConfig.goal_planner_min_ready() == 2
    assert AutonomyConfig.goal_planner_cooldown_sec() == 3600
    assert AutonomyConfig.goal_planner_history_n() == 10
    assert AutonomyConfig.goal_daily_quota() == 6
    assert AutonomyConfig.goal_self_wake_enabled() is False
    assert AutonomyConfig.goal_dedup_threshold() == 0.6


def test_overrides(monkeypatch):
    monkeypatch.setenv("GOAL_PLANNER_ENABLED", "true")
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "3")
    monkeypatch.setenv("GOAL_SELF_WAKE_ENABLED", "true")
    assert AutonomyConfig.goal_planner_enabled() is True
    assert AutonomyConfig.goal_daily_quota() == 3
    assert AutonomyConfig.goal_self_wake_enabled() is True
