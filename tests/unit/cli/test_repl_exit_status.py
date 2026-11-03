"""098 CL-07: a REPL exit keeps a finished status; it cancels only a live run."""
from types import SimpleNamespace

import pytest

from agents.task.agent.session import SessionManager
from cli.repl_exit import close_on_exit, needs_cancel


@pytest.fixture
def sm(tmp_path):
    return SessionManager(base_dir=str(tmp_path))


def _ui(active):
    return SimpleNamespace(lifecycle=SimpleNamespace(is_active=lambda: active))


def test_clean_exit_keeps_completed(sm):
    sid = sm.create_session("cl07-done", user_id="u1")
    sm.update_session_status(sid, "running")
    sm.update_session_status(sid, "completed")
    assert needs_cancel(_ui(False), sm, sid) is False
    assert close_on_exit(sm, sid) is False
    assert sm.get_session_info(sid)["status"] == "completed"


def test_sync_cleanup_flips_created_to_cancelled(sm):
    sid = sm.create_session("cl07-new", user_id="u1")
    assert close_on_exit(sm, sid) is True
    assert sm.get_session_info(sid)["status"] == "cancelled"


def test_sync_cleanup_does_not_overwrite_failed(sm):
    sid = sm.create_session("cl07-fail", user_id="u1")
    sm.update_session_status(sid, "running")
    sm.update_session_status(sid, "failed")
    assert close_on_exit(sm, sid) is False
    assert sm.get_session_info(sid)["status"] == "failed"


def test_live_turn_or_running_status_needs_cancel(sm):
    sid = sm.create_session("cl07-live", user_id="u1")
    assert needs_cancel(_ui(True), sm, sid) is True
    sm.update_session_status(sid, "running")
    assert needs_cancel(_ui(False), sm, sid) is True
    assert needs_cancel(None, sm, sid) is True      # unreadable state: cancel as before
