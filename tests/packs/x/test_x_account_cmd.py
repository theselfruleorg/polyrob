"""polyrob x-account — capture-session / status (Task 10, 2026-08-18 plan)."""
import pytest

pytest.importorskip("polyrob_x")

from click.testing import CliRunner


def test_x_account_registered_lazily():
    from cli.polyrob import cli
    assert "x-account" in cli.list_commands(None)


def test_x_account_is_group_with_subcommands():
    from polyrob_x.commands.x_account import x_account
    names = set(x_account.commands)
    assert {"capture-session", "status"} <= names


def test_status_empty_store(monkeypatch, tmp_path):
    from polyrob_x.commands import x_account as mod

    class _Store:
        def load(self, uid):
            return None

        def exists(self, uid):
            return False

    monkeypatch.setattr(mod, "_store", lambda: _Store())
    monkeypatch.setattr(mod, "_user_id", lambda: "u1")
    res = CliRunner().invoke(mod.x_account, ["status"])
    assert res.exit_code == 0, res.output
    assert "no x" in res.output.lower() or "not captured" in res.output.lower()


def test_status_with_session(monkeypatch):
    from polyrob_x.commands import x_account as mod

    class _Store:
        def load(self, uid):
            return {"handle": "robbot", "storage_state": {}, "created_at": "2026-08-18"}

        def exists(self, uid):
            return True

    monkeypatch.setattr(mod, "_store", lambda: _Store())
    monkeypatch.setattr(mod, "_user_id", lambda: "u1")
    res = CliRunner().invoke(mod.x_account, ["status"])
    assert res.exit_code == 0, res.output
    assert "robbot" in res.output


def test_lazy_import_layering():
    # The command must not be imported at cli.polyrob module load — only listed.
    import importlib
    import sys
    sys.modules.pop("polyrob_x.commands.x_account", None)
    importlib.reload(importlib.import_module("cli.polyrob"))
    assert "polyrob_x.commands.x_account" not in sys.modules


def test_signup_subcommand_registered():
    from polyrob_x.commands.x_account import x_account
    assert "signup" in x_account.commands


def test_capture_completion_names_browser_dm_rail(monkeypatch):
    """The capture command must not imply the API poller can consume the session."""
    import inspect
    from polyrob_x.commands import x_account as mod

    source = inspect.getsource(mod._capture)
    assert "X_BROWSER_ENABLED" in source      # via core.remedy (026 P4)
    assert "x_read_dms" in source


def test_signup_refuses_when_account_exists(monkeypatch):
    from polyrob_x.commands import x_account as mod

    class _Store:
        def exists(self, uid):
            return True

        def load(self, uid):
            return {"storage_state": {}, "handle": "already"}

    monkeypatch.setattr(mod, "_store", lambda: _Store())
    monkeypatch.setattr(mod, "_user_id", lambda: "u1")
    # No display -> headless branch; account-exists check runs first and exits 1.
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    res = CliRunner().invoke(mod.x_account, ["signup"])
    assert res.exit_code == 1
    assert "already exists" in res.output.lower()


# --- import-session / capture-session --out (2026-09-19) -----------------------
# The store is Fernet-encrypted with THIS box's MCP_ENCRYPTION_KEY and keyed by
# THIS box's identity, so a session captured on a laptop cannot simply be copied
# to the VPS. The bridge is a plain Playwright storage_state JSON: capture writes
# it with --out, import reads it (or just the two X login cookies) and stores it
# under the server's key.

class _RecStore:
    def __init__(self):
        self.saved = None

    def load(self, uid):
        return None

    def exists(self, uid):
        return False

    def save(self, uid, **kw):
        self.saved = (uid, kw)


def test_import_session_registered():
    from polyrob_x.commands.x_account import x_account
    assert "import-session" in x_account.commands


def test_import_session_from_storage_state_file(monkeypatch, tmp_path):
    import json
    from polyrob_x.commands import x_account as mod
    st = {"cookies": [{"name": "auth_token", "value": "a" * 40, "domain": ".x.com",
                       "path": "/", "secure": True, "httpOnly": True, "sameSite": "None",
                       "expires": -1},
                      {"name": "ct0", "value": "b" * 32, "domain": ".x.com", "path": "/",
                       "secure": True, "httpOnly": False, "sameSite": "Lax", "expires": -1}],
          "origins": []}
    f = tmp_path / "x-session.json"
    f.write_text(json.dumps(st))
    store = _RecStore()
    monkeypatch.setattr(mod, "_store", lambda: store)
    monkeypatch.setattr(mod, "_user_id", lambda: "rob")
    res = CliRunner().invoke(mod.x_account, ["import-session", str(f), "--handle", "robbot"])
    assert res.exit_code == 0, res.output
    uid, kw = store.saved
    assert uid == "rob"
    assert kw["storage_state"] == st
    assert kw["handle"] == "robbot"
    assert "imported" in res.output.lower()


def test_import_session_from_cookie_values(monkeypatch):
    from polyrob_x.commands import x_account as mod
    store = _RecStore()
    monkeypatch.setattr(mod, "_store", lambda: store)
    monkeypatch.setattr(mod, "_user_id", lambda: "rob")
    res = CliRunner().invoke(mod.x_account, ["import-session", "--cookies"],
                             input="x" * 40 + "\n" + "y" * 32 + "\n")
    assert res.exit_code == 0, res.output
    _, kw = store.saved
    names = {c["name"]: c for c in kw["storage_state"]["cookies"]}
    assert "x" * 40 not in res.output and "y" * 32 not in res.output
    assert names["auth_token"]["value"] == "x" * 40
    assert names["ct0"]["value"] == "y" * 32
    assert names["auth_token"]["domain"] == ".x.com"
    assert names["auth_token"]["httpOnly"] is True
    assert kw["storage_state"]["origins"] == []


def test_import_session_refuses_file_without_login_cookie(monkeypatch, tmp_path):
    import json
    from polyrob_x.commands import x_account as mod
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"cookies": [{"name": "guest_id", "value": "1", "domain": ".x.com",
                                          "path": "/"}], "origins": []}))
    store = _RecStore()
    monkeypatch.setattr(mod, "_store", lambda: store)
    monkeypatch.setattr(mod, "_user_id", lambda: "rob")
    res = CliRunner().invoke(mod.x_account, ["import-session", str(f)])
    assert res.exit_code == 1
    assert "auth_token" in res.output
    assert store.saved is None


def test_import_session_needs_a_source(monkeypatch):
    from polyrob_x.commands import x_account as mod
    store = _RecStore()
    monkeypatch.setattr(mod, "_store", lambda: store)
    monkeypatch.setattr(mod, "_user_id", lambda: "rob")
    res = CliRunner().invoke(mod.x_account, ["import-session"])
    assert res.exit_code != 0
    assert store.saved is None


def test_capture_session_has_out_option():
    from polyrob_x.commands.x_account import x_account
    params = {p.name for p in x_account.commands["capture-session"].params}
    assert "out" in params


# --- the owner CLI acts on the SERVICE's home/key/owner (2026-09-26 P1-4) ------

def _stub_store(monkeypatch, mod):
    class _Store:
        def load(self, uid):
            return None

        def exists(self, uid):
            return False

    monkeypatch.setattr(mod, "_store", lambda: _Store())
    monkeypatch.setattr(mod, "_user_id", lambda: "u1")


def test_deployed_box_refuses_without_the_service_key_and_owner(monkeypatch):
    from cli import _admin_home as ah
    from polyrob_x.commands import x_account as mod
    _stub_store(monkeypatch, mod)
    monkeypatch.setattr(ah, "_deployed_box", lambda: True)
    monkeypatch.delenv("MCP_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    monkeypatch.setattr("cli.commands._bootstrap.ensure_env_loaded", lambda *a, **k: None)
    res = CliRunner().invoke(mod.x_account, ["oauth-status"])
    assert res.exit_code != 0
    assert "MCP_ENCRYPTION_KEY" in res.output and "POLYROB_OWNER_USER_ID" in res.output
    assert ". /etc/polyrob/polyrob.env" in res.output
    assert "sudo -E -u polyrob-agent" in res.output


def test_deployed_box_binds_the_admin_data_home(monkeypatch, tmp_path):
    from cli import _admin_home as ah
    from polyrob_x.commands import x_account as mod
    _stub_store(monkeypatch, mod)
    seen = {}
    monkeypatch.setattr(ah, "_deployed_box", lambda: True)

    def _home(write=None):
        seen["write"] = write
        return str(tmp_path)
    monkeypatch.setattr(ah, "admin_data_dir", _home)
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", "k")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setattr("cli.commands._bootstrap.ensure_env_loaded", lambda *a, **k: None)
    res = CliRunner().invoke(mod.x_account, ["status"])
    assert res.exit_code == 0, res.output
    import os
    assert os.environ["POLYROB_DATA_DIR"] == str(tmp_path)
    assert seen["write"] is False                       # status is a read


def test_local_box_is_untouched(monkeypatch):
    from cli import _admin_home as ah
    from polyrob_x.commands import x_account as mod
    _stub_store(monkeypatch, mod)
    monkeypatch.setattr(ah, "_deployed_box", lambda: False)
    monkeypatch.delenv("MCP_ENCRYPTION_KEY", raising=False)
    res = CliRunner().invoke(mod.x_account, ["status"])
    assert res.exit_code == 0, res.output


def test_oauth_status_shows_relogin_needed():
    from polyrob_x.commands.x_account import _oauth_status_lines
    lines = _oauth_status_lines({"stored": True, "expired": True, "expires_in_sec": -5,
                                 "has_refresh_token": True, "relogin_needed": True,
                                 "relogin_needed_since": "09-26 04:01Z (3d)",
                                 "relogin_remedy": "/x login"})
    text = "\n".join(lines)
    assert "re-login needed" in text and "09-26 04:01Z" in text and "/x login" in text


def test_cookie_cli_does_not_offer_secret_argv_options():
    from polyrob_x.commands.x_account import x_account
    names = {p.name for p in x_account.commands["import-session"].params}
    assert "auth_token" not in names and "ct0" not in names
    assert "cookies" in names


def test_plain_session_export_does_not_follow_link(tmp_path):
    import json
    import stat
    from polyrob_x.commands.x_account import _write_plain_state
    victim = tmp_path / "victim"
    victim.write_text("keep")
    output = tmp_path / "session.json"
    output.symlink_to(victim)
    _write_plain_state(str(output), {"cookies": []})
    assert victim.read_text() == "keep"
    assert not output.is_symlink()
    assert json.loads(output.read_text()) == {"cookies": []}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_plain_session_export_replaces_public_mode(tmp_path):
    import stat
    from polyrob_x.commands.x_account import _write_plain_state
    output = tmp_path / "session.json"
    output.write_text("old")
    output.chmod(0o644)
    _write_plain_state(str(output), {"cookies": []})
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
