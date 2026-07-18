"""019 P3 — session-list activity badge from the in-process RunActivity snapshot."""
import pytest

from agents.task.telemetry import run_activity


@pytest.fixture(autouse=True)
def _clean():
    run_activity._reset_for_tests()
    yield
    run_activity._reset_for_tests()


class _Route:
    is_missing = False
    is_remote = False


class _Agent:
    def route_session(self, session_id):
        return _Route()


def test_annotate_runtime_adds_activity_badge(monkeypatch):
    from webview import server

    run_activity.note_feed_event("sessA", "tool_started", {"action_name": "navigate"})
    monkeypatch.setattr(server, "_in_process_task_agent", lambda: _Agent())
    rows = [{"id": "sessA", "status": "running"}]
    out = server._annotate_runtime(rows)
    assert out[0]["activity"] == {"phase": "tool", "detail": "navigate"}


def test_annotate_runtime_omits_idle_and_unknown(monkeypatch):
    from webview import server

    run_activity.note_feed_event("sessB", "tool_started", {"action_name": "x"})
    run_activity.note_feed_event("sessB", "tool_execution", {})  # → idle
    monkeypatch.setattr(server, "_in_process_task_agent", lambda: _Agent())
    rows = [{"id": "sessB", "status": "running"},
            {"id": "sessUnknown", "status": "running"}]
    out = server._annotate_runtime(rows)
    assert "activity" not in out[0]  # idle → no badge
    assert "activity" not in out[1]  # unknown session → honest absence
