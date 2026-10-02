"""026 P5 — live apply where safe, never for money / approval / ingress / frozen."""
import os

import pytest

from core.config_policy.live_apply import LIVE_APPLY_SAFE, live_apply_allowed, never_live
from core.config_service import _IMPORT_FROZEN_FLAGS, set_value


def test_scope_is_the_autonomy_loop_and_posture_groups():
    from core.config_policy.autonomy_posture import _POSTURE_FULL_FLAGS
    from core.config_policy.local_profile import _AUTONOMY_LOCAL_FLAGS
    assert LIVE_APPLY_SAFE == (set(_AUTONOMY_LOCAL_FLAGS) | set(_POSTURE_FULL_FLAGS)
                               | {"AUTONOMY_ENABLED", "AUTONOMY_POSTURE"})
    assert all(live_apply_allowed(k) for k in LIVE_APPLY_SAFE), \
        [k for k in LIVE_APPLY_SAFE if not live_apply_allowed(k)]


@pytest.mark.parametrize("key", sorted(_IMPORT_FROZEN_FLAGS) + [
    "PAYMENT_APPROVAL_MODE", "APPROVAL_REQUIRED_TOOLS", "WALLET_ENABLED",
    "WALLET_DAILY_CAP_USD", "X402_INVOICE_ENABLED", "DEFI_TRADE_ENABLED",
    "TELEGRAM_SURFACE_ENABLED", "CORRESPONDENT_ACCESS_ENABLED",
    "AUTONOMY_MODE", "POLYROB_LOCAL", "AGENT_COMPUTE_POSTURE",
    "DEFI_BRIDGE_ENABLED", "BRIDGE_WATCHER_ENABLED",
])
def test_money_approval_ingress_and_frozen_flags_are_never_live(key):
    assert never_live(key)
    assert not live_apply_allowed(key)


def test_no_live_member_is_money_approval_or_ingress_shaped():
    assert not [k for k in LIVE_APPLY_SAFE if never_live(k)]


def _iso(monkeypatch, tmp_path, *keys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    for k in keys:
        # setenv first so monkeypatch RESTORES the var: a live write sets
        # os.environ directly.
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)


def test_live_write_sets_the_process_env(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path, "GOALS_ENABLED")
    res = set_value("GOALS_ENABLED", "true", scope="project", surface="local", live=True)
    assert res.ok and res.live and res.applies == "live"
    assert os.environ["GOALS_ENABLED"] == "true"
    assert "live in this session" in res.message


def test_live_propagates_to_the_runtime_gate(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path, "GOALS_ENABLED")
    from core.config_policy import AutonomyConfig
    set_value("GOALS_ENABLED", "false", scope="project", surface="local", live=True)
    assert AutonomyConfig.goals_enabled() is False
    set_value("GOALS_ENABLED", "true", scope="project", surface="local", live=True)
    assert AutonomyConfig.goals_enabled() is True


def test_payment_approval_mode_is_never_live(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path, "PAYMENT_APPROVAL_MODE")
    res = set_value("PAYMENT_APPROVAL_MODE", "auto", scope="project",
                    surface="local", live=True)
    assert res.ok and not res.live
    assert "PAYMENT_APPROVAL_MODE" not in os.environ
    assert "restart" in res.message


def test_remote_surface_never_live(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path, "GOALS_ENABLED")
    res = set_value("GOALS_ENABLED", "true", scope="project", surface="console", live=True)
    assert not res.live
    assert "GOALS_ENABLED" not in os.environ


def test_not_live_unless_asked(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path, "GOALS_ENABLED")
    res = set_value("GOALS_ENABLED", "true", scope="project", surface="local")
    assert res.ok and not res.live
    assert "GOALS_ENABLED" not in os.environ
