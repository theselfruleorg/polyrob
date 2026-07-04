"""WS-A/B: `polyrob owner invite` seeds a correspondent (owner-driven bootstrap)."""
import os

from click.testing import CliRunner

from cli.commands.owner import owner
from core.surfaces.correspondents import CorrespondentRegistry


def test_owner_data_dir_default_matches_daemon(tmp_path, monkeypatch):
    # With no POLYROB_DATA_DIR, owner admin must resolve <cwd>/.polyrob (what the
    # surface daemon's container.config.data_dir uses), NOT the old divergent ./data.
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    from cli.commands.owner import _data_dir
    from core.bootstrap import _resolve_cli_data_home
    dd = _data_dir()
    assert dd != "data"
    assert dd.endswith(".polyrob")
    assert dd == str(_resolve_cli_data_home()[0])  # same resolver the daemon uses


def test_invite_seeds_then_approve_activates(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "true")
    runner = CliRunner()

    r = runner.invoke(owner, ["invite", "email", "john@acme.com", "sess1",
                              "--user", "u_owner"])
    assert r.exit_code == 0, r.output
    assert "pending" in r.output

    reg = CorrespondentRegistry(os.path.join(str(tmp_path), "correspondents.db"))
    assert reg.resolve(surface="email", address="john@acme.com") is None  # pending

    r2 = runner.invoke(owner, ["approve", "email", "john@acme.com"])
    assert r2.exit_code == 0, r2.output
    row = reg.resolve(surface="email", address="john@acme.com")
    assert row is not None and row["session_id"] == "sess1"
