"""031 T11: polyrob autonomy pause|halt|resume|status over the one record."""
import json

from click.testing import CliRunner


def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setattr("core.bootstrap.load_env", lambda *a, **k: None)


def test_pause_resume_status_roundtrip(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    from cli.commands.autonomy import autonomy
    r = CliRunner()
    out = r.invoke(autonomy, ["pause", "cron", "--for", "90m"])
    assert out.exit_code == 0, out.output
    assert "Paused cron" in out.output and "90 min" in out.output
    st = r.invoke(autonomy, ["status", "--json"])
    assert st.exit_code == 0, st.output
    payload = json.loads(st.output)
    assert payload["pause"]["paused"] is True and payload["pause"]["scopes"] == ["cron"]
    assert payload["pause"]["via"] == "cli" and payload["halted"] is False
    text = r.invoke(autonomy, ["status"])
    assert "pause: ⏸ PAUSED (cron)" in text.output
    out = r.invoke(autonomy, ["resume"])
    assert out.exit_code == 0 and "RESUMED" in out.output
    assert json.loads(r.invoke(autonomy, ["status", "--json"]).output)["pause"]["paused"] is False
    assert "pause: ▶ RUNNING" in r.invoke(autonomy, ["status"]).output


def test_halt_is_an_alias_and_bad_scope_is_an_error(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    from cli.commands.autonomy import autonomy
    r = CliRunner()
    out = r.invoke(autonomy, ["halt"])
    assert out.exit_code == 0 and "Paused everything" in out.output
    assert json.loads(r.invoke(autonomy, ["status", "--json"]).output)["halted"] is True
    bad = r.invoke(autonomy, ["pause", "bogus"])
    assert bad.exit_code != 0 and "unknown scope" in bad.output
