"""2026-08-28 intel finding: the 08-26 entry-pause directive lived only as
prose (ledger + each goal's own reasoning), so a stream-manifest goal that
never referenced it traded through it unnoticed. `owner_admin.pause_entries`/
`resume_entries` + `AutonomyConfig.entry_paused()` give it a single structural
source of truth, mirroring the existing halt/resume kill-switch exactly.
"""
from click.testing import CliRunner


def test_entry_paused_reads_resolved_data_home(tmp_path, monkeypatch):
    monkeypatch.delenv("TREASURY_ENTRY_PAUSE", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core.config_policy import AutonomyConfig
    assert AutonomyConfig.entry_paused() is False
    (tmp_path / "TREASURY_ENTRY_PAUSE").write_text("")
    assert AutonomyConfig.entry_paused() is True


def test_entry_paused_env_flag(monkeypatch):
    monkeypatch.setenv("TREASURY_ENTRY_PAUSE", "true")
    from core.config_policy import AutonomyConfig
    assert AutonomyConfig.entry_paused() is True


def test_owner_pause_entries_and_resume_toggle_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    from cli.commands.owner import owner
    runner = CliRunner()

    r = runner.invoke(owner, ["pause-entries"])
    assert r.exit_code == 0, r.output
    from core.autonomy_control import PAUSE_FILENAME, read_state
    assert (tmp_path / PAUSE_FILENAME).exists()  # 031: the trading SCOPE of the one record
    assert read_state(str(tmp_path)).scopes == ("trading",)
    assert "Paused trading" in r.output

    r = runner.invoke(owner, ["resume-entries"])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / PAUSE_FILENAME).exists()
    assert "RESUMED" in r.output


def test_resume_entries_reports_no_file_case(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)
    from cli.commands.owner import owner
    runner = CliRunner()
    r = runner.invoke(owner, ["resume-entries"])
    assert r.exit_code == 0, r.output
    assert "not paused" in r.output
