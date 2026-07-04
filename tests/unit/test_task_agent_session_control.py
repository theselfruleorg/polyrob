"""TaskAgent.pause_session / resume_session — honest status-transition ops.

These are the methods the `rob session pause` / `rob session resume` CLI commands
call. They must do an ownership check and a valid status transition, mirroring
cancel_session's shape, without touching a live execution loop.

The suite uses ``asyncio.run`` inside sync tests (the repo's pattern — pytest.ini
does not enable pytest-asyncio auto mode).
"""
import asyncio


class _SM:
    """Minimal SessionManager stand-in recording attempted transitions."""

    def __init__(self, status, user_id="u1"):
        self._info = {"user_id": user_id, "status": status}
        self.transitions = []

    def get_session_info(self, sid):
        return dict(self._info)

    def try_transition_status(self, sid, frm, to):
        self.transitions.append((frm, to))
        # mirror session.py validity: RUNNING->SUSPENDED and *->RESUMED allowed here
        return to in ("suspended", "resumed")


def _agent(sm):
    from agents.task_agent_lite import TaskAgent

    a = TaskAgent.__new__(TaskAgent)  # bypass heavy __init__
    a.task_available = True
    a.session_manager = sm
    return a


def test_pause_running_suspends():
    sm = _SM("running")
    a = _agent(sm)
    assert asyncio.run(a.pause_session(user_id="u1", session_id="s")) is True
    assert sm.transitions == [("running", "suspended")]


def test_pause_wrong_owner_denied():
    sm = _SM("running", user_id="owner")
    a = _agent(sm)
    assert asyncio.run(a.pause_session(user_id="intruder", session_id="s")) is False
    assert sm.transitions == []  # never attempted a transition


def test_pause_unknown_session_denied():
    sm = _SM("running")
    sm.get_session_info = lambda sid: {}  # not found
    a = _agent(sm)
    assert asyncio.run(a.pause_session(user_id="u1", session_id="s")) is False


def test_resume_suspended_ok():
    sm = _SM("suspended")
    a = _agent(sm)
    assert asyncio.run(a.resume_session(user_id="u1", session_id="s")) is True
    assert sm.transitions == [("suspended", "resumed")]


def test_resume_created_denied():
    """A freshly-created (never-run) session cannot be 'resumed'."""
    sm = _SM("created")
    a = _agent(sm)
    assert asyncio.run(a.resume_session(user_id="u1", session_id="s")) is False
    assert sm.transitions == []


def test_pause_unavailable_agent_returns_false():
    sm = _SM("running")
    a = _agent(sm)
    a.task_available = False
    assert asyncio.run(a.pause_session(user_id="u1", session_id="s")) is False
