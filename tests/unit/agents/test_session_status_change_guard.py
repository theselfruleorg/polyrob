"""A10 — ``update_session_status`` change guard.

A session end used to call ``update_session_status`` twice (and cancel three
times) with the SAME status, so every status event appeared 2-3x in the
activity feed (both the invalid-status string path and the enum path called
``_emit_status_event`` unconditionally). The guard added right after the
existence check in ``agents/task/agent/session.py::update_session_status``
skips the save + emit entirely when the normalized new status equals the
current one, and both branches below it inherit the guard.
"""
from unittest.mock import MagicMock

from agents.task.agent.session import SessionManager


def _sm(tmp_path):
    return SessionManager(base_dir=str(tmp_path))


def test_identical_status_update_emits_once(tmp_path):
    sm = _sm(tmp_path)
    sid = sm.create_session("sess-identical", user_id="u1")
    sm._emit_status_event = MagicMock()

    sm.update_session_status(sid, "running")
    sm.update_session_status(sid, "running")  # exact repeat — must no-op

    assert sm._emit_status_event.call_count == 1
    assert sm._sessions[sid]["status"] == "running"


def test_identical_status_update_via_enum_emits_once(tmp_path):
    """The guard must also fire when the repeat arrives as a SessionStatus
    enum rather than a raw string (the two call shapes production code uses)."""
    from agents.task.agent.session import SessionStatus

    sm = _sm(tmp_path)
    sid = sm.create_session("sess-identical-enum", user_id="u1")
    sm._emit_status_event = MagicMock()

    sm.update_session_status(sid, SessionStatus.RUNNING)
    sm.update_session_status(sid, SessionStatus.RUNNING)

    assert sm._emit_status_event.call_count == 1


def test_real_status_change_emits_once(tmp_path):
    sm = _sm(tmp_path)
    sid = sm.create_session("sess-change", user_id="u1")
    sm._emit_status_event = MagicMock()

    sm.update_session_status(sid, "running")  # created -> running: a real change

    assert sm._emit_status_event.call_count == 1


def test_created_running_completed_emits_three_times(tmp_path):
    sm = _sm(tmp_path)
    sid = sm.create_session("sess-lifecycle", user_id="u1")
    # create_session already stamps status='created'; blank the in-memory
    # status so the explicit 'created' call below is itself a genuine change
    # (the scenario under test is three DISTINCT transitions, not a session
    # freshly created by create_session).
    sm._sessions[sid]["status"] = None
    sm._emit_status_event = MagicMock()

    sm.update_session_status(sid, "created")
    sm.update_session_status(sid, "running")
    sm.update_session_status(sid, "completed")

    assert sm._emit_status_event.call_count == 3


def test_invalid_status_string_path_also_guarded(tmp_path):
    """The raw-string fallback branch (status.lower() not a valid
    SessionStatus member) must inherit the guard too — repeating an invalid
    status string emits only once."""
    sm = _sm(tmp_path)
    sid = sm.create_session("sess-invalid", user_id="u1")
    sm._emit_status_event = MagicMock()

    sm.update_session_status(sid, "totally-not-a-real-status")
    sm.update_session_status(sid, "totally-not-a-real-status")

    assert sm._emit_status_event.call_count == 1
    assert sm._sessions[sid]["status"] == "totally-not-a-real-status"


def test_cancel_called_three_times_emits_once(tmp_path):
    """Mirrors the reported symptom: cancel calling update_session_status
    three times in a row for the same terminal status."""
    sm = _sm(tmp_path)
    sid = sm.create_session("sess-cancel", user_id="u1")
    sm._emit_status_event = MagicMock()

    sm.update_session_status(sid, "cancelled")
    sm.update_session_status(sid, "cancelled")
    sm.update_session_status(sid, "cancelled")

    assert sm._emit_status_event.call_count == 1
