"""067 P1b: the regime shape in the kernel, the keys from the rail."""
import core.money.regime as regime


def test_the_defi_keys_are_registered_in_order():
    import core.config_policy.money_regime  # noqa: F401 — registers
    assert regime.arming_keys() == (
        "DEFI_AGENT_AUTONOMY", "DEFI_TRADE_ENABLED", "DEFI_AUTONOMOUS_TURN_TRADING",
        "DEFI_TIERED_SPEND_LANE", "WALLET_DAILY_CAP_USD")


def test_supervised_ignores_the_keys(monkeypatch):
    monkeypatch.setattr(regime, "_ARMING", {"K": lambda: False})
    assert regime.resolve_regime(False) == (regime.REGIME_SUPERVISED, ())


def test_a_raising_check_is_unset(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(regime, "_ARMING", {"A": lambda: True, "B": boom})
    assert regime.resolve_regime(True) == (regime.REGIME_AUTONOMOUS, ("B",))


def test_all_set_is_armed(monkeypatch):
    monkeypatch.setattr(regime, "_ARMING", {"A": lambda: True})
    assert regime.resolve_regime(True).name == regime.REGIME_ARMED


def test_no_rail_registered_is_never_armed(monkeypatch):
    monkeypatch.setattr(regime, "_ARMING", {})
    assert regime.resolve_regime(True) == (regime.REGIME_AUTONOMOUS, ())


def test_autonomous_work_enabled_moved_and_is_re_exported(monkeypatch):
    import importlib
    autonomy_mode = importlib.import_module("core.config_policy.autonomy_mode")
    money_regime = importlib.import_module("core.config_policy.money_regime")
    assert money_regime.autonomous_work_enabled is autonomy_mode.autonomous_work_enabled
    for value in (True, False):
        monkeypatch.setattr(autonomy_mode, "full_autonomy_enabled", lambda v=value: v)
        assert autonomy_mode.autonomous_work_enabled() is value
