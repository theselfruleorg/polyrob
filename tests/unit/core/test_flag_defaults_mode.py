"""026 P0.1 — dynamic_flag_default must know the AUTONOMY_MODE capability group.

The gap (G4): `doctor --flags` / `/config get` / `explain` reported the 8
mode-governed capability flags with their static OFF defaults even when
`AUTONOMY_MODE=autonomous` resolved them ON at runtime — the report lied about
the single most consequential preset lever. `flag_defaults.dynamic_flag_default`
now resolves them through `_mode_capability_default` with an honest
`default(mode:...)` label, and gives AUTONOMY_ENABLED a label that says its
default is derived, not static.
"""
import pytest

from core.config_policy import policy
from core.config_policy.flag_defaults import dynamic_flag_default
from core.config_policy.policy import _MODE_CAPABILITY_FLAGS


@pytest.fixture(autouse=True)
def _clean_mode_env(monkeypatch):
    for var in ("AUTONOMY_MODE", "AUTONOMY_POSTURE", "AUTONOMY_ENABLED",
                "POLYROB_LOCAL", "ROB_LOCAL", "POLYROB_OWNER_USER_ID",
                "POLYROB_OWNER_EMAIL", "POLYROB_OWNER_TELEGRAM_ID",
                "BOT_OWNER_USER_ID", "BOT_OWNER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    policy.reset_autonomy_mode_warnings()
    yield
    policy.reset_autonomy_mode_warnings()


def _bind_effective_autonomous(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-uid")
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")


def test_mode_flags_resolve_on_under_effective_autonomous(monkeypatch):
    _bind_effective_autonomous(monkeypatch)
    assert policy.full_autonomy_enabled() is True
    for name in sorted(_MODE_CAPABILITY_FLAGS):
        dyn = dynamic_flag_default(name)
        assert dyn is not None, f"{name} must resolve dynamically"
        value, label = dyn
        assert value is True, f"{name} must report ON under effective autonomous"
        assert label == "default(mode:autonomous)"


def test_mode_flags_resolve_off_under_supervised():
    for name in sorted(_MODE_CAPABILITY_FLAGS):
        dyn = dynamic_flag_default(name)
        assert dyn is not None
        value, label = dyn
        assert value is False
        assert label == "default(mode:supervised)"


def test_mode_flags_off_when_autonomous_clamped(monkeypatch):
    # autonomous requested but no owner bound -> clamped -> flags stay OFF,
    # and the label says supervised (the EFFECTIVE mode), never autonomous.
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    dyn = dynamic_flag_default("X402_INVOICE_ENABLED")
    assert dyn == (False, "default(mode:supervised)")


def test_autonomy_enabled_gets_derived_label(monkeypatch):
    dyn = dynamic_flag_default("AUTONOMY_ENABLED")
    assert dyn is not None
    value, label = dyn
    assert value is False
    assert label == "default(mode/posture-derived)"
    monkeypatch.setenv("AUTONOMY_POSTURE", "owner-visible")
    value, label = dynamic_flag_default("AUTONOMY_ENABLED")
    assert value is True
    assert label == "default(mode/posture-derived)"


def test_doctor_flags_report_shows_mode_resolution(monkeypatch):
    """End-to-end through flags_report: the report line must show ON + mode label."""
    from cli.commands.doctor import flags_report
    _bind_effective_autonomous(monkeypatch)
    lines = flags_report({"POLYROB_LOCAL": "1", "AUTONOMY_MODE": "autonomous",
                          "POLYROB_OWNER_USER_ID": "owner-uid"})
    row = next(ln for ln in lines if ln.strip().startswith("X402_INVOICE_ENABLED "))
    assert "True" in row and "mode:autonomous" in row
