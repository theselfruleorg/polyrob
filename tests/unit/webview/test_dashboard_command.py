"""Tests for the `polyrob dashboard` (webgate) CLI command."""
import os

import pytest
from click.testing import CliRunner

_POSTURE_ENV_KEYS = ("POLYROB_POSTURE", "WEBGATE_MULTITENANT", "WEBGATE_HOST",
                      "WEBGATE_PORT", "WEBVIEW_HOST", "WEBVIEW_PORT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _POSTURE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield
    # dashboard() mutates os.environ directly (e.g. --host feeds WEBGATE_HOST
    # for posture derivation, B5) rather than through monkeypatch, so a value
    # it sets during the test is NOT auto-reverted by monkeypatch's teardown
    # when the key was absent beforehand (delenv(raising=False) on an absent
    # key registers no undo). Clean up for real so later test files/modules
    # in the same pytest session don't inherit a leaked posture/host.
    for k in _POSTURE_ENV_KEYS:
        os.environ.pop(k, None)


def test_dashboard_help_lists_command():
    from cli.commands.dashboard import dashboard
    result = CliRunner().invoke(dashboard, ["--help"])
    assert result.exit_code == 0
    assert "webgate" in result.output.lower() or "dashboard" in result.output.lower()


def test_dashboard_registered_in_cli():
    # The CLI lazy-loads subcommands (_LazyGroup) — click's eager `.commands`
    # dict holds only the non-lazy few, so assert through the group's real
    # resolution path (list_commands/get_command), same as `polyrob --help`.
    import click

    from cli.polyrob import cli
    ctx = click.Context(cli)
    assert "dashboard" in cli.list_commands(ctx)
    assert cli.get_command(ctx, "dashboard") is not None
    assert cli.get_command(ctx, "webgate") is not None  # alias


def test_dashboard_binds_loopback_by_default(monkeypatch):
    captured = {}

    import uvicorn
    monkeypatch.setattr(
        uvicorn, "run",
        lambda app, **kw: captured.update(host=kw.get("host"), port=kw.get("port")),
    )

    from cli.commands.dashboard import dashboard
    result = CliRunner().invoke(dashboard, ["--no-browser"])
    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 5050


def test_dashboard_multitenant_binds_all_interfaces(monkeypatch):
    captured = {}

    import uvicorn
    monkeypatch.setattr(
        uvicorn, "run",
        lambda app, **kw: captured.update(host=kw.get("host"), port=kw.get("port")),
    )

    from cli.commands.dashboard import dashboard
    result = CliRunner().invoke(dashboard, ["--no-browser", "--multitenant"])
    assert result.exit_code == 0, result.output
    assert captured["host"] == "0.0.0.0"


def test_dashboard_host_port_override(monkeypatch):
    captured = {}

    import uvicorn
    monkeypatch.setattr(
        uvicorn, "run",
        lambda app, **kw: captured.update(host=kw.get("host"), port=kw.get("port")),
    )

    from cli.commands.dashboard import dashboard
    result = CliRunner().invoke(
        dashboard, ["--no-browser", "--host", "0.0.0.0", "--port", "9099"]
    )
    assert result.exit_code == 0, result.output
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9099


def test_dashboard_leaves_no_data_dir_in_an_empty_cwd(monkeypatch, tmp_path):
    """With no DATA_ROOT / POLYROB_DATA_DIR the console's session tree anchors on
    the data home (<cwd>/.polyrob/sessions, the tree `polyrob` writes), so the
    legacy ./data/task is never created in the caller's folder."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    try:
        from cli.commands.dashboard import dashboard
        result = CliRunner().invoke(dashboard, ["--no-browser"])
        assert result.exit_code == 0, result.output

        from agents.task.path import PathManager
        from core.runtime_paths import resolve_data_home, resolve_session_data_root
        root = resolve_session_data_root()
        assert root == (resolve_data_home() / "sessions").resolve()
        PathManager()  # what the console startup builds
        assert not (tmp_path / "data").exists()
    finally:
        os.environ.pop("DATA_ROOT", None)


def test_dashboard_keeps_an_operator_data_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "mine"))
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    from cli.commands.dashboard import dashboard
    assert CliRunner().invoke(dashboard, ["--no-browser"]).exit_code == 0
    assert os.environ["DATA_ROOT"] == str(tmp_path / "mine")
