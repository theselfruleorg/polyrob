"""036: `polyrob rails` — the terminal seat over the ONE /rail implementation."""
import yaml
from click.testing import CliRunner

from cli.commands import rails as rails_cmd


def _seat(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setattr(rails_cmd, "_seat", lambda: ("rob", str(tmp_path)))
    monkeypatch.setattr("cli.commands._bootstrap.ensure_env_loaded", lambda: None)


def test_new_list_export_import_roundtrip(tmp_path, monkeypatch):
    _seat(tmp_path, monkeypatch)
    r = CliRunner()
    out = r.invoke(rails_cmd.rails, ["new", "daily-digest", "time=07:30"])
    assert out.exit_code == 0 and "ON" in out.output
    assert "daily-digest" in r.invoke(rails_cmd.rails, ["list"]).output
    exported = r.invoke(rails_cmd.rails, ["export"])
    doc = yaml.safe_load(exported.output)
    assert doc["streams"][0]["id"] == "daily-digest"
    manifest = tmp_path / "rails.yaml"
    manifest.write_text(yaml.safe_dump({"streams": [{
        "id": "ship", "schedule": "every 12h", "objective": {"title": "Ship"},
        "goals": [{"title": "build", "body": "b", "tools": ["shell"]}]}]}))
    imp = r.invoke(rails_cmd.rails, ["import", str(manifest)])
    assert imp.exit_code == 0 and "PENDING" in imp.output and "ship:shell" in imp.output


def test_grant_needs_yes(tmp_path, monkeypatch):
    _seat(tmp_path, monkeypatch)
    r = CliRunner()
    r.invoke(rails_cmd.rails, ["new", "ship-something"])
    assert "confirm" in r.invoke(rails_cmd.rails, ["grant", "ship-something", "shell"]).output
    assert "Granted shell" in r.invoke(
        rails_cmd.rails, ["grant", "ship-something", "shell", "--yes"]).output


def test_import_refuses_a_session_workspace_file(tmp_path, monkeypatch):
    _seat(tmp_path, monkeypatch)
    ws = tmp_path / "sessions" / "s1" / "workspace"
    ws.mkdir(parents=True)
    f = ws / "evil.yaml"
    f.write_text("streams: []")
    out = CliRunner().invoke(rails_cmd.rails, ["import", str(f)])
    assert out.exit_code != 0 and "session workspace" in out.output
