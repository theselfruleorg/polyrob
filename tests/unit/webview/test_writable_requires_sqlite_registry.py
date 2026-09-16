"""043 W8 — a writable console needs the shared session registry.

The console carries its OWN `TaskAgent` (`server.py`, preferred over the `:9000`
hop when the task router is mounted in-process). With the default in-memory
session registry that agent knows nothing about the sessions `polyrob.service`
owns, so a console message to a live session RESUMES it here: two processes
stepping the same session, two message histories, one workspace.

`SESSION_REGISTRY_BACKEND=sqlite` is what makes that impossible — the session
resolves REMOTE and the console answers an honest 409 with the owner's pid
instead of quietly taking over (`api/session_routing.py`). So it is the
precondition for `WEBVIEW_READ_ONLY=false`, checked at boot.
"""
import importlib

import pytest


@pytest.fixture()
def guard(monkeypatch):
    monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    monkeypatch.delenv("SESSION_REGISTRY_BACKEND", raising=False)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u-owner")   # W7 satisfied
    import webview.webgate as wg
    importlib.reload(wg)
    from webview import posture_guard
    return posture_guard


_BOUND = {"POLYROB_OWNER_USER_ID": "u-owner"}


def test_a_writable_console_without_the_shared_registry_refuses(guard):
    with pytest.raises(RuntimeError) as exc:
        guard.assert_writable_console(env=_BOUND, posture="own_ops")
    assert ("a writable console needs SESSION_REGISTRY_BACKEND=sqlite so it "
            "cannot resume a session the agent service owns") in str(exc.value)


def test_the_memory_backend_is_named_explicitly_too(guard):
    with pytest.raises(RuntimeError):
        guard.assert_writable_console(
            env=dict(_BOUND, SESSION_REGISTRY_BACKEND="memory"), posture="own_ops")


def test_sqlite_satisfies_it(guard):
    guard.assert_writable_console(
        env=dict(_BOUND, SESSION_REGISTRY_BACKEND="sqlite"), posture="own_ops")


def test_case_and_whitespace_do_not_defeat_it(guard):
    guard.assert_writable_console(
        env=dict(_BOUND, SESSION_REGISTRY_BACKEND=" SQLite "), posture="own_ops")


def test_a_read_only_console_is_unaffected(guard, monkeypatch):
    """Prod today: read-only, in-memory registry. Nothing to refuse — the
    console cannot resume anything because it cannot mutate anything."""
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    guard.assert_writable_console(env=_BOUND, posture="own_ops")


def test_a_local_workstation_console_is_warned_not_refused(guard, caplog):
    """`polyrob dashboard` on a laptop is usually the ONLY agent process, so
    there is no second writer to race — refusing would break first run. The risk
    is still stated once, because a laptop CAN also be running `polyrob telegram`."""
    from webview import webgate
    webgate._warned_shared_registry = False
    with caplog.at_level("WARNING"):
        guard.assert_writable_console(env=_BOUND, posture="local")
    assert "SESSION_REGISTRY_BACKEND=sqlite" in caplog.text


def test_the_startup_hook_runs_the_check():
    import inspect
    import webview.server as srv
    assert "assert_writable_console" in inspect.getsource(srv.startup_event)
