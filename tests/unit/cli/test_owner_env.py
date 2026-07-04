"""The `polyrob owner` group must bootstrap env before subcommands read config.

Regression: the group callback never called ``load_env``, so file-set config
(written by ``polyrob config set …``) was invisible — ``owner show`` reported
"unbound" even after setting POLYROB_OWNER_USER_ID, and ``owner invite`` couldn't
honour a file-set CORRESPONDENT_ACCESS_ENABLED. Every sibling long-running command
(serve/dashboard/kb/model) loads env first; ``owner`` now matches.
"""
import core.bootstrap as bootstrap
from cli.commands.owner import owner
from click.testing import CliRunner


def test_owner_group_invokes_load_env(monkeypatch):
    """The group callback must call load_env(local_mode=True) before subcommands run."""
    calls = {}

    def _spy_load_env(*args, **kwargs):
        calls["called"] = True
        calls["local_mode"] = kwargs.get("local_mode")

    monkeypatch.setattr(bootstrap, "load_env", _spy_load_env)
    monkeypatch.setattr(bootstrap, "setup_project_path", lambda: "")
    monkeypatch.setattr(bootstrap, "setup_sqlite_compat", lambda: None)

    result = CliRunner().invoke(owner, ["show"])

    assert result.exit_code == 0, result.output
    assert calls.get("called") is True, "owner group did not call load_env"
    assert calls.get("local_mode") is True, "owner group must load env with local_mode=True"


def test_owner_show_reflects_file_set_owner(monkeypatch):
    """`owner show` must reflect owner config that load_env brings in from the .env layer.

    Simulate file-set config by having the (bootstrap) load_env populate the env var
    the way reading config/.env.* would — proving the wiring is what makes it visible.
    """
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("BOT_OWNER_USER_ID", raising=False)

    def _load_env_from_file(*args, **kwargs):
        # Stand in for `load_env` reading a file-set POLYROB_OWNER_USER_ID.
        monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-from-file")

    monkeypatch.setattr(bootstrap, "load_env", _load_env_from_file)
    monkeypatch.setattr(bootstrap, "setup_project_path", lambda: "")
    monkeypatch.setattr(bootstrap, "setup_sqlite_compat", lambda: None)

    result = CliRunner().invoke(owner, ["show"])

    assert result.exit_code == 0, result.output
    assert "owner-from-file" in result.output
    assert "unbound" not in result.output
