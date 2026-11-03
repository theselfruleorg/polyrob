"""098 AG-14: one session status table, enforced by both writers.

Owner decision Q1 (098 close-out): no state is terminal — "we need to be able to
resume anything". A new message after a cancel resumes the session
(``cancelled -> resumed`` / ``-> initializing`` / ``-> running``)."""
import json
from unittest.mock import MagicMock

import pytest

from agents.task.agent.session import SESSION_MACHINE, SessionManager
from agents.task.task_agent_control import TaskAgentControlMixin
from core.state import machine as sm_mod


@pytest.fixture
def sm(tmp_path):
    return SessionManager(base_dir=str(tmp_path))


class _Ctl(TaskAgentControlMixin):
    def __init__(self, sm):
        self.session_manager = sm
        self.task_available = True


def test_machine_well_formed():
    assert SESSION_MACHINE.validate() == []
    assert SESSION_MACHINE.allowed("cancelled", "running")      # Q1: a new message resumes
    assert SESSION_MACHINE.normalize("error") == "failed"


def test_every_state_reaches_running():
    """Q1: no state is terminal — each state has a path to ``running``."""
    assert SESSION_MACHINE.terminal == frozenset()
    for start in SESSION_MACHINE.states:
        seen, todo = {start}, [start]
        while todo:
            cur = todo.pop()
            for nxt in SESSION_MACHINE.transitions.get(cur, frozenset()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        assert "running" in seen or start == "running", start
    for st in ("failed", "suspended", "completed", "cancelled"):
        assert SESSION_MACHINE.allowed(st, "resumed"), st
        assert SESSION_MACHINE.allowed(st, "initializing"), st


@pytest.mark.asyncio
async def test_resume_session_accepts_cancelled(sm):
    sid = sm.create_session("s-resume-cancel", user_id="u1")
    sm.update_session_status(sid, "running")
    sm.update_session_status(sid, "cancelled")
    ctl = _Ctl(sm)
    assert await ctl.resume_session("u1", sid) is True
    assert sm.get_session_info(sid)["status"] == "resumed"
    assert sm.try_transition_status(sid, "resumed", "running")


def test_completion_cas_only_from_running(sm):
    """A late completion after a cancel loses, also from a resumed state."""
    sid = sm.create_session("s-late", user_id="u1")
    sm.update_session_status(sid, "running")
    sm.update_session_status(sid, "cancelled")
    sm.update_session_status(sid, "resumed")
    ctl = _Ctl(sm)
    assert ctl._finish_status(sid, "completed") is False
    assert sm.get_session_info(sid)["status"] == "resumed"


def test_illegal_edge_refused_by_both_writers(sm, monkeypatch):
    monkeypatch.setattr(sm_mod, "_STRICT", False)
    sid = sm.create_session("s-illegal", user_id="u1")
    assert sm.update_session_status(sid, "running")
    assert sm.update_session_status(sid, "completed")
    assert sm.update_session_status(sid, "running") is False      # completed -> running
    assert sm.try_transition_status(sid, "completed", "running") is False
    assert sm.get_session_info(sid)["status"] == "completed"


@pytest.mark.asyncio
async def test_pause_only_from_running(sm):
    sid = sm.create_session("s-pause", user_id="u1")
    sm.update_session_status(sid, "cancelled")
    ctl = _Ctl(sm)
    assert await ctl.pause_session("u1", sid) is False
    assert sm.get_session_info(sid)["status"] == "cancelled"
    sm.update_session_status(sid, "running")                       # resumed by a message
    assert await ctl.pause_session("u1", sid) is True
    assert sm.get_session_info(sid)["status"] == "suspended"


def test_cancel_during_run_not_overwritten_by_completed(sm):
    sid = sm.create_session("s-race", user_id="u1")
    sm.update_session_status(sid, "running")
    sm.update_session_status(sid, "cancelled")                     # owner cancel mid-run
    ctl = _Ctl(sm)
    assert ctl._finish_status(sid, "completed") is False
    assert sm.get_session_info(sid)["status"] == "cancelled"


def test_finish_status_wins_from_running(sm):
    sid = sm.create_session("s-done", user_id="u1")
    sm.update_session_status(sid, "running")
    assert _Ctl(sm)._finish_status(sid, "completed") is True
    assert sm.get_session_info(sid)["status"] == "completed"


def test_boot_flip_running_to_suspended_is_persisted(tmp_path):
    meta = tmp_path / "u1" / "s-boot" / "metadata.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"id": "s-boot", "user_id": "u1", "status": "running"}))
    mgr = SessionManager(base_dir=str(tmp_path))
    assert mgr.get_session_info("s-boot")["status"] == "suspended"
    assert json.loads(meta.read_text())["status"] == "suspended"


def test_try_transition_emits_status_event(sm):
    sid = sm.create_session("s-evt", user_id="u1")
    sm._emit_status_event = MagicMock()
    sm._save_status_summary = MagicMock()
    assert sm.try_transition_status(sid, "created", "running")
    sm._emit_status_event.assert_called_once_with(sid, "running", "created")
    sm._save_status_summary.assert_called_once_with(sid)


def test_cleanup_never_rewrites_cancelled():
    from agents.task.session import cleanup
    src = open(cleanup.__file__).read()
    assert "TERMINAL_STATUSES" not in src and "SESSION_MACHINE.allowed" in src


def test_metadata_written_atomically(sm, monkeypatch):
    import agents.task.agent.session as session_mod
    calls = []
    monkeypatch.setattr(session_mod, "atomic_write_json", lambda p, o, **k: calls.append(p))
    sm.create_session("s-atomic", user_id="u1")
    assert calls and calls[-1].endswith("metadata.json")
