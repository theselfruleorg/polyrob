"""RUN_BUDGET_USD accessor truth-table (T1.1 run-budget gate)."""
from core.config_policy.policy import run_budget_usd


def test_default_is_disabled(monkeypatch):
    monkeypatch.delenv("RUN_BUDGET_USD", raising=False)
    assert run_budget_usd() == 0.0


def test_env_value_read_live(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "2.50")
    assert run_budget_usd() == 2.50
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    assert run_budget_usd() == 0.10  # live read — no import-time freeze


def test_junk_and_disable_idioms_fall_back_to_default(monkeypatch):
    for raw in ("abc", "", "  ", "off", "none"):
        monkeypatch.setenv("RUN_BUDGET_USD", raw)
        assert run_budget_usd() == 0.0, raw
