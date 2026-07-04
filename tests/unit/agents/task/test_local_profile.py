"""Tests for the POLYROB_LOCAL terminal-native profile (safe autonomy flags ON as a group).

The flag readers (`local_mode_enabled`, `AutonomyConfig.*_enabled`) read the environment
LIVE at call time via `_bool_env`/`os.getenv`, so these tests just monkeypatch env and call
the functions directly. Do NOT `importlib.reload(constants)` here — reloading swaps the
global module object and desyncs other tests that imported names from it (it caused
order-dependent failures in unrelated suites).
"""
import agents.task.constants as constants


def test_rob_local_alias_enables_local_mode(monkeypatch):
    # ROB_LOCAL is a deprecated back-compat alias for POLYROB_LOCAL (older docs/scripts
    # said ROB_LOCAL) — it must NOT be a silent no-op.
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("ROB_LOCAL", "1")
    assert constants.local_mode_enabled() is True


def test_local_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("ROB_LOCAL", raising=False)
    monkeypatch.delenv("GOALS_ENABLED", raising=False)
    monkeypatch.delenv("CURATOR_ENABLED", raising=False)
    monkeypatch.delenv("SELF_WAKE_ENABLED", raising=False)
    assert constants.local_mode_enabled() is False
    # safe flags stay OFF when not local
    assert constants.AutonomyConfig.goals_enabled() is False
    assert constants.AutonomyConfig.curator_enabled() is False
    assert constants.AutonomyConfig.self_wake_enabled() is False


def test_local_mode_flips_safe_flags_on(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    for v in ("GOALS_ENABLED", "CURATOR_ENABLED", "SELF_WAKE_ENABLED",
              "SKILLS_WRITABLE", "BACKGROUND_REVIEW_ENABLED", "INSIGHTS_TOOL"):
        monkeypatch.delenv(v, raising=False)
    assert constants.local_mode_enabled() is True
    assert constants.AutonomyConfig.goals_enabled() is True
    assert constants.AutonomyConfig.curator_enabled() is True
    assert constants.AutonomyConfig.self_wake_enabled() is True
    assert constants.AutonomyConfig.skills_writable() is True
    assert constants.AutonomyConfig.background_review_enabled() is True
    assert constants.AutonomyConfig.insights_tool() is True


def test_explicit_off_overrides_local(monkeypatch):
    # an explicit disable must beat the local default
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("GOALS_ENABLED", "off")
    assert constants.AutonomyConfig.goals_enabled() is False


def test_code_exec_not_flipped_by_local(monkeypatch):
    # code execution is NOT a safe-local flag — local mode must never enable it
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("CODE_EXEC_ENABLED", raising=False)
    assert "CODE_EXEC_ENABLED" not in constants._SAFE_LOCAL_FLAGS


def test_ticker_idle_backoff_off_by_default(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("TICKER_IDLE_BACKOFF_ENABLED", raising=False)
    assert constants.ticker_idle_backoff_enabled() is False


def test_ticker_idle_backoff_on_under_local_mode(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("TICKER_IDLE_BACKOFF_ENABLED", raising=False)
    assert constants.ticker_idle_backoff_enabled() is True


def test_ticker_idle_backoff_explicit_off_beats_local_mode(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("TICKER_IDLE_BACKOFF_ENABLED", "off")
    assert constants.ticker_idle_backoff_enabled() is False


def test_ticker_idle_backoff_max_multiplier_default(monkeypatch):
    monkeypatch.delenv("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", raising=False)
    assert constants.ticker_idle_backoff_max_multiplier() == 5


def test_ticker_idle_backoff_max_multiplier_explicit(monkeypatch):
    monkeypatch.setenv("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", "3")
    assert constants.ticker_idle_backoff_max_multiplier() == 3
