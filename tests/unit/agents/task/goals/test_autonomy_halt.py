"""P0-4 (2026-07-09): the owner kill-switch that halts all autonomous spend/dispatch."""
from core.config_policy import AutonomyConfig


def test_autonomy_halted_env(monkeypatch):
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    assert AutonomyConfig.autonomy_halted() is True
    monkeypatch.setenv("AUTONOMY_HALT", "false")
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    assert AutonomyConfig.autonomy_halted() is False


def test_autonomy_halted_file(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    assert AutonomyConfig.autonomy_halted() is False
    (tmp_path / "AUTONOMY_HALT").write_text("")
    assert AutonomyConfig.autonomy_halted() is True
