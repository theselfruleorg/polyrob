"""H6 (2026-07-15): the owner kill-switch must actually be usable.

The docstring for AutonomyConfig.autonomy_halted() promised `polyrob owner halt`/
`resume` — commands that did not exist — and the file probe only read
POLYROB_DATA_DIR/DATA_ROOT, never the RESOLVED data home, so a halt file placed at a
local install's data home (cwd/.polyrob) was never seen.
"""
from click.testing import CliRunner


def test_autonomy_halted_reads_resolved_data_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.autonomy_halted() is False
    (tmp_path / "AUTONOMY_HALT").write_text("")
    assert AutonomyConfig.autonomy_halted() is True


def test_owner_halt_and_resume_toggle_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    from cli.commands.owner import owner
    runner = CliRunner()

    r = runner.invoke(owner, ["halt"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "AUTONOMY_HALT").exists()

    r = runner.invoke(owner, ["resume"])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / "AUTONOMY_HALT").exists()
