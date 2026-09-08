"""polyrob apps list/show/approve/reject/kill/logs — the CLI seat (032)."""
import json

from click.testing import CliRunner


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SERVICES_DB_PATH", str(tmp_path / "app_services.db"))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    from core.app_service.registry import AppServiceRegistry
    r = AppServiceRegistry(str(tmp_path / "app_services.db"))
    r.upsert_request("st", "owner-1", source_dir="/p", cmd=["x"], container_port=80, health_path="/",
                     egress="none", egress_allow=[], env={}, workspace_digest="d" * 64)
    return r


def test_cli_verbs(tmp_path, monkeypatch):
    from cli.commands.apps import apps
    r = _seed(tmp_path, monkeypatch)
    run = CliRunner()
    res = run.invoke(apps, ["list", "--user", "owner-1"])
    assert res.exit_code == 0 and "st [pending]" in res.output
    res = run.invoke(apps, ["list", "--user", "owner-1", "--json"])
    assert json.loads(res.output)[0]["slug"] == "st"
    res = run.invoke(apps, ["approve", "st", "--user", "owner-1"])
    assert res.exit_code == 0 and "approved" in res.output
    assert r.get("st", "owner-1")["status"] == "approved"
    res = run.invoke(apps, ["approve", "st", "--user", "owner-1"])
    assert res.exit_code == 1 and "not pending" in res.output
    res = run.invoke(apps, ["show", "st", "--user", "owner-1"])
    assert res.exit_code == 0 and "st [approved]" in res.output
    res = run.invoke(apps, ["logs", "st", "--user", "owner-1"])
    assert res.exit_code == 0 and "no logs yet" in res.output
    res = run.invoke(apps, ["kill", "st", "--user", "owner-1"])
    assert res.exit_code == 0 and r.get("st", "owner-1")["status"] == "stopped"
    res = run.invoke(apps, ["reject", "st", "--user", "owner-1"])
    assert res.exit_code == 1
