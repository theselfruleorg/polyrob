"""C4: a client may supply a custom session_id on create (`POST /api/task/sessions`
and A2A `message/send`). If that id already belongs to a DIFFERENT user,
TaskAgent.create_session must REFUSE — otherwise it builds a fresh orchestrator over
the victim's (DoS) and stomps their task/model/tools metadata (cross-user hijack).

Tests the chokepoint guard directly (both HTTP and A2A funnel through it).
"""
import pytest

from agents.task_agent_lite import TaskAgent
from core.exceptions import SessionOwnershipError


class _SM:
    """Minimal SessionManager stand-in exposing only get_session_info."""

    def __init__(self, sessions):
        self._s = sessions

    def get_session_info(self, session_id):
        return self._s.get(session_id, {})


def _ta(sessions):
    ta = TaskAgent.__new__(TaskAgent)
    ta.session_manager = _SM(sessions)
    return ta


def test_reuse_of_another_users_session_id_is_refused():
    ta = _ta({"s1": {"user_id": "tenant-a"}})
    with pytest.raises(SessionOwnershipError):
        ta._assert_session_owner("s1", "tenant-b")


def test_owner_can_reuse_own_session_id():
    ta = _ta({"s1": {"user_id": "tenant-a"}})
    ta._assert_session_owner("s1", "tenant-a")  # must not raise


def test_new_session_id_is_allowed():
    ta = _ta({})
    ta._assert_session_owner("s-new", "tenant-b")  # must not raise


def test_none_session_id_is_allowed():
    ta = _ta({})
    ta._assert_session_owner(None, "tenant-b")  # generated id path — must not raise


def test_session_without_recorded_owner_is_allowed():
    # Legacy/unowned session — mirror _require_session_owner's None-owner allowance.
    ta = _ta({"s1": {}})
    ta._assert_session_owner("s1", "tenant-b")  # must not raise
