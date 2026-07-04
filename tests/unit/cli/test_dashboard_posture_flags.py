"""Tests for `polyrob dashboard`'s posture flags (B5).

Mirrors the CliRunner convention already used in
tests/unit/webview/test_dashboard_command.py. These tests mutate real
os.environ (matching dashboard()'s own os.environ.setdefault/os.environ[...]
style — it isn't parametrized for injection); isolate with an autouse
monkeypatch.delenv fixture per test.
"""
import os

import pytest
from click.testing import CliRunner
from unittest.mock import patch


_POSTURE_ENV_KEYS = ("POLYROB_POSTURE", "WEBGATE_MULTITENANT", "WEBGATE_HOST",
                      "WEBGATE_PORT", "WEBVIEW_HOST", "WEBVIEW_PORT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _POSTURE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield
    # dashboard() mutates os.environ directly rather than through monkeypatch
    # (existing module constraint — see test_dashboard_command.py), so clean
    # up for real to avoid leaking posture/host state into later test files.
    for k in _POSTURE_ENV_KEYS:
        os.environ.pop(k, None)


def test_dashboard_default_sets_no_posture_env():
    from cli.commands.dashboard import dashboard
    runner = CliRunner()
    with patch("uvicorn.run"), patch("webbrowser.open"):
        runner.invoke(dashboard, ["--no-browser"])
    assert os.environ.get("POLYROB_POSTURE") in (None, "local")


def test_dashboard_posture_own_ops_flag_sets_env():
    from cli.commands.dashboard import dashboard
    runner = CliRunner()
    with patch("uvicorn.run"), patch("webbrowser.open"):
        runner.invoke(dashboard, ["--posture", "own_ops", "--no-browser"])
    assert os.environ.get("POLYROB_POSTURE") == "own_ops"


def test_dashboard_multitenant_flag_still_maps_to_posture_2():
    from cli.commands.dashboard import dashboard
    runner = CliRunner()
    with patch("uvicorn.run"), patch("webbrowser.open"):
        runner.invoke(dashboard, ["--multitenant", "--no-browser"])
    assert os.environ.get("WEBGATE_MULTITENANT") == "true"


def test_dashboard_host_0000_without_posture_derives_own_ops():
    """MANDATORY AMENDMENT (safe-by-default fix): a non-loopback --host with
    no --posture must NOT stay in Posture 0 (no-auth). --host must feed
    WEBGATE_HOST before `from webview.server import app` so
    webgate.posture()'s host-derivation (loopback -> local, else -> own_ops)
    actually sees the bind address the CLI is about to use.
    """
    from cli.commands.dashboard import dashboard
    from webview import webgate

    runner = CliRunner()
    with patch("uvicorn.run"), patch("webbrowser.open"):
        result = runner.invoke(dashboard, ["--host", "0.0.0.0", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert webgate.posture() == "own_ops"


def test_dashboard_posture_flag_wins_over_host():
    """Explicit --posture always wins outright over host-derivation (matches
    webgate.posture()'s documented resolution order)."""
    from cli.commands.dashboard import dashboard
    from webview import webgate

    runner = CliRunner()
    with patch("uvicorn.run"), patch("webbrowser.open"):
        result = runner.invoke(
            dashboard, ["--host", "0.0.0.0", "--posture", "local", "--no-browser"]
        )

    assert result.exit_code == 0, result.output
    assert webgate.posture() == "local"
