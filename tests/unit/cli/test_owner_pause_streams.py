"""2026-09-02 incident: an owner "stop all ghosts" directive (repeated twice)
cancelled every live goal, but the standing streams (data/streams/streams.yaml)
had no code-level awareness of it and kept reseeding on their own cadence — one
cycle ran to full completion before anyone caught it. `owner_admin.pause_stream_
seeding`/`resume_stream_seeding` + `AutonomyConfig.stream_seeding_paused()` give
the seeder a single structural source of truth, mirroring the existing
pause-entries/halt-resume kill-switches exactly.
"""
from click.testing import CliRunner


def test_stream_seeding_paused_reads_resolved_data_home(tmp_path, monkeypatch):
    monkeypatch.delenv("STREAM_SEEDING_PAUSE", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core.config_policy import AutonomyConfig
    assert AutonomyConfig.stream_seeding_paused() is False
    (tmp_path / "STREAM_SEEDING_PAUSE").write_text("")
    assert AutonomyConfig.stream_seeding_paused() is True


def test_stream_seeding_paused_env_flag(monkeypatch):
    monkeypatch.setenv("STREAM_SEEDING_PAUSE", "true")
    from core.config_policy import AutonomyConfig
    assert AutonomyConfig.stream_seeding_paused() is True


def test_owner_pause_streams_and_resume_toggle_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    from cli.commands.owner import owner
    runner = CliRunner()

    from core.autonomy_control import PAUSE_FILENAME, read_state
    r = runner.invoke(owner, ["pause-streams"])
    assert r.exit_code == 0, r.output
    # 031: the streams pause is a SCOPE of the one record, not a second file.
    assert (tmp_path / PAUSE_FILENAME).exists()
    assert read_state(str(tmp_path)).scopes == ("streams",)
    assert "Paused streams" in r.output

    r = runner.invoke(owner, ["resume-streams"])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / PAUSE_FILENAME).exists()
    assert "RESUMED" in r.output


def test_resume_streams_reports_no_file_case(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    from cli.commands.owner import owner
    runner = CliRunner()
    r = runner.invoke(owner, ["resume-streams"])
    assert r.exit_code == 0, r.output
    assert "not paused" in r.output
