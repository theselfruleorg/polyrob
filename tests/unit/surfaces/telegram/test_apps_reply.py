"""/apps on Telegram (032): joins the three lists and renders the shared owner text."""
from surfaces.telegram.apps_ops import apps_reply


def test_apps_joins_the_three_lists():
    from core.surfaces.dispatcher import _COMMANDS
    from surfaces.telegram.harness import _HELP_BODY, _OWNER_ADMIN_COMMANDS, help_commands
    assert "/apps" in _OWNER_ADMIN_COMMANDS and "/apps" in _COMMANDS and "/apps" in _HELP_BODY
    assert "apps" in {name for name, _ in help_commands()}


def test_apps_reply_verbs(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SERVICES_DB_PATH", str(tmp_path / "app_services.db"))
    from core.app_service.registry import AppServiceRegistry
    r = AppServiceRegistry(str(tmp_path / "app_services.db"))
    assert apps_reply("u1", str(tmp_path), []) == "No apps."
    r.upsert_request("st", "u1", source_dir="/p", cmd=["x"], container_port=80, health_path="/",
                     egress="none", egress_allow=[], env={}, workspace_digest="d" * 64)
    assert "st [pending]" in apps_reply("u1", str(tmp_path), ["list"])
    assert apps_reply("u1", str(tmp_path), ["approve"]) == "Usage: /apps approve <slug>"
    assert "approved" in apps_reply("u1", str(tmp_path), ["approve", "st"])
    assert r.get("st", "u1")["status"] == "approved"
    assert "st [approved]" in apps_reply("u1", str(tmp_path), ["show", "st"])
    assert "no logs yet" in apps_reply("u1", str(tmp_path), ["logs", "st"])
    assert "stop queued" in apps_reply("u1", str(tmp_path), ["kill", "st"])
    assert apps_reply("u1", str(tmp_path), ["dance"]).startswith("Usage:")
