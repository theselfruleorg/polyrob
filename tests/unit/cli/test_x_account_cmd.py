"""polyrob x-account — capture-session / status (Task 10, 2026-08-18 plan)."""
from click.testing import CliRunner


def test_x_account_registered_lazily():
    from cli.polyrob import cli
    assert "x-account" in cli.list_commands(None)


def test_x_account_is_group_with_subcommands():
    from cli.commands.x_account import x_account
    names = set(x_account.commands)
    assert {"capture-session", "status"} <= names


def test_status_empty_store(monkeypatch, tmp_path):
    from cli.commands import x_account as mod

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
    from cli.commands import x_account as mod

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
    sys.modules.pop("cli.commands.x_account", None)
    importlib.reload(importlib.import_module("cli.polyrob"))
    assert "cli.commands.x_account" not in sys.modules


def test_signup_subcommand_registered():
    from cli.commands.x_account import x_account
    assert "signup" in x_account.commands


def test_capture_completion_names_browser_dm_rail(monkeypatch):
    """The capture command must not imply the API poller can consume the session."""
    import inspect
    from cli.commands import x_account as mod

    source = inspect.getsource(mod._capture)
    assert "X_BROWSER_ENABLED=true" in source
    assert "x_read_dms" in source


def test_signup_refuses_when_account_exists(monkeypatch):
    from cli.commands import x_account as mod

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
    from cli.commands.x_account import x_account
    assert "import-session" in x_account.commands


def test_import_session_from_storage_state_file(monkeypatch, tmp_path):
    import json
    from cli.commands import x_account as mod
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
    from cli.commands import x_account as mod
    store = _RecStore()
    monkeypatch.setattr(mod, "_store", lambda: store)
    monkeypatch.setattr(mod, "_user_id", lambda: "rob")
    res = CliRunner().invoke(mod.x_account, ["import-session", "--auth-token", "x" * 40,
                                             "--ct0", "y" * 32])
    assert res.exit_code == 0, res.output
    _, kw = store.saved
    names = {c["name"]: c for c in kw["storage_state"]["cookies"]}
    assert names["auth_token"]["value"] == "x" * 40
    assert names["ct0"]["value"] == "y" * 32
    assert names["auth_token"]["domain"] == ".x.com"
    assert names["auth_token"]["httpOnly"] is True
    assert kw["storage_state"]["origins"] == []


def test_import_session_refuses_file_without_login_cookie(monkeypatch, tmp_path):
    import json
    from cli.commands import x_account as mod
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
    from cli.commands import x_account as mod
    store = _RecStore()
    monkeypatch.setattr(mod, "_store", lambda: store)
    monkeypatch.setattr(mod, "_user_id", lambda: "rob")
    res = CliRunner().invoke(mod.x_account, ["import-session"])
    assert res.exit_code != 0
    assert store.saved is None


def test_capture_session_has_out_option():
    from cli.commands.x_account import x_account
    params = {p.name for p in x_account.commands["capture-session"].params}
    assert "out" in params
