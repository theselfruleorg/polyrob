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
