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


def test_budget_aware_autonomy_flag(monkeypatch):
    for var in ("BUDGET_AWARE_AUTONOMY", "AUTONOMY_BUDGET_USD",
                "AUTONOMY_BUDGET_WINDOW_DAYS", "AUTONOMY_POSTURE", "POLYROB_LOCAL"):
        monkeypatch.delenv(var, raising=False)
    # default OFF (silent posture, no local)
    assert AutonomyConfig.budget_aware_autonomy() is False
    monkeypatch.setenv("BUDGET_AWARE_AUTONOMY", "true")
    assert AutonomyConfig.budget_aware_autonomy() is True
    assert AutonomyConfig.autonomy_budget_usd() == 10.0  # default
    monkeypatch.setenv("AUTONOMY_BUDGET_USD", "25")
    assert AutonomyConfig.autonomy_budget_usd() == 25.0
    monkeypatch.setenv("AUTONOMY_BUDGET_USD", "not-a-number")
    assert AutonomyConfig.autonomy_budget_usd() == 10.0  # fallback
    assert AutonomyConfig.autonomy_budget_window_days() == 1
    monkeypatch.setenv("AUTONOMY_BUDGET_WINDOW_DAYS", "3")
    assert AutonomyConfig.autonomy_budget_window_days() == 3


def test_budget_aware_on_under_owner_visible_posture(monkeypatch):
    monkeypatch.delenv("BUDGET_AWARE_AUTONOMY", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("AUTONOMY_POSTURE", "owner-visible")
    assert AutonomyConfig.budget_aware_autonomy() is True
