from core.app_service.owner_ops import APPS_EMPTY_LINE
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
    assert apps_reply("u1", str(tmp_path), []) == APPS_EMPTY_LINE
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


# ---------------------------------------------------------------------------
# The audit trail must name the seat that ran the verb, not the phone.
# ---------------------------------------------------------------------------

def test_the_seat_reaches_the_audit_trail(monkeypatch):
    """`via` was the literal "telegram", so a REPL or CLI approve/kill was
    recorded as having come from the phone — the one field whose whole job is
    to say WHERE an owner decision was made."""
    from core.app_service import owner_ops as core_ops
    from surfaces.telegram import apps_ops

    seen = {}

    def _approve(reg, slug, user_id, via="?"):
        seen["via"] = via
        return True, "approved"

    monkeypatch.setattr(core_ops, "approve", _approve)
    monkeypatch.setattr(
        apps_ops, "__doc__", apps_ops.__doc__)      # no-op, keeps linters quiet

    apps_ops.apps_reply("rob", "/tmp", ["approve", "slug"])
    assert seen["via"] == "telegram"                # the default is unchanged

    apps_ops.apps_reply("rob", "/tmp", ["approve", "slug"], via="repl")
    assert seen["via"] == "repl"


def test_every_deciding_verb_carries_the_seat(monkeypatch):
    from core.app_service import owner_ops as core_ops
    from surfaces.telegram import apps_ops

    seen = []

    def _record(name):
        def _fn(reg, slug, user_id, via="?"):
            seen.append((name, via))
            return True, name
        return _fn

    for verb in ("approve", "reject", "kill"):
        monkeypatch.setattr(core_ops, verb, _record(verb))
    for verb in ("approve", "reject", "kill"):
        apps_ops.apps_reply("rob", "/tmp", [verb, "slug"], via="console")
    assert seen == [("approve", "console"), ("reject", "console"),
                    ("kill", "console")]
