"""098 TS-21: one projection of a session onto an A2A task state, a message to
a non-resident task is delivered (or refused — never resumed without it), and
the session-metadata mirror is the one push-config copy."""
import asyncio

import pytest

import api.a2a.task_handler as th
from api.a2a.models import A2AMessage, A2ATaskState, PushNotificationConfig
from api.a2a.task_handler import A2ATaskHandler, a2a_state


class _SM:
    def __init__(self, info):
        self.info = info

    def update_session_metadata(self, sid, upd):
        self.info.update(upd)


class _Agent:
    def __init__(self, status, deliver="delivered"):
        self._info = {"user_id": "u1", "status": status, "metadata": {}, "config": {},
                      "created_at": "", "updated_at": ""}
        self.session_manager = _SM(self._info)
        self.delivered, self.runs = [], []
        self._deliver = deliver

    async def get_session_by_id(self, task_id):
        return dict(self._info)

    async def ensure_session_and_deliver(self, user_id, sid, text, *, kind, metadata=None):
        if self._deliver == "delivered":
            self.delivered.append((sid, text, kind))
        return self._deliver

    async def run_session(self, user_id, sid):
        self.runs.append(sid)


class _Container:
    def __init__(self, agent):
        self.agent = agent

    def get_agent(self, name):
        return self.agent

    def get_service(self, name):
        return None


def _setup(monkeypatch, status, deliver="delivered", resident=None):
    async def _resolve(sid, agent):
        return resident
    monkeypatch.setattr(th, "resolve_orchestrator", _resolve)
    import api.session_routing as sr
    monkeypatch.setattr(sr, "guard_remote", lambda agent, sid: None)
    import agents.task.billed_request as br
    monkeypatch.setattr(br, "validate_billed_session", lambda info: None)
    spawned = []
    monkeypatch.setattr(th, "_spawn_session_task", lambda coro: (spawned.append(1), coro.close()))
    agent = _Agent(status, deliver)
    return A2ATaskHandler(_Container(agent)), agent, spawned


def _msg(text="the answer"):
    return A2AMessage(role="user", parts=[{"kind": "text", "text": text}])


def test_send_message_to_nonresident_task_is_delivered(monkeypatch):
    h, agent, spawned = _setup(monkeypatch, "suspended")
    asyncio.run(h.send_message("t1", _msg(), "u1"))
    assert agent.delivered == [("t1", "the answer", "a2a_message")]
    assert spawned == [1]


def test_send_message_to_nonresident_task_is_refused_when_not_accepted(monkeypatch):
    h, agent, spawned = _setup(monkeypatch, "suspended", deliver="gone")
    with pytest.raises(ValueError, match="not accepted"):
        asyncio.run(h.send_message("t1", _msg(), "u1"))
    assert spawned == []        # never resumed without the message


def test_resume_only_from_suspended(monkeypatch):
    class _Orch:
        def __init__(self):
            self.msgs = []

        async def submit_user_message(self, **kw):
            self.msgs.append(kw["text"])

    orch = _Orch()
    h, agent, spawned = _setup(monkeypatch, "running", resident=orch)
    asyncio.run(h.send_message("t1", _msg(), "u1"))
    assert orch.msgs == ["the answer"] and spawned == []
    h, agent, spawned = _setup(monkeypatch, "suspended", resident=_Orch())
    asyncio.run(h.send_message("t1", _msg(), "u1"))
    assert spawned == [1]


def test_get_task_reports_input_required_during_approval(monkeypatch):
    h, agent, _ = _setup(monkeypatch, "running")
    monkeypatch.setattr(th, "_current_activity", lambda sid: {"phase": "awaiting_approval"})
    task = asyncio.run(h.get_task("t1", history_length=0))
    assert task.status.state == A2ATaskState.INPUT_REQUIRED
    monkeypatch.setattr(th, "_current_activity", lambda sid: {"phase": "tool"})
    assert asyncio.run(h.get_task("t1", history_length=0)).status.state == A2ATaskState.WORKING


def test_projection_never_overrides_a_terminal_state():
    assert a2a_state("completed", {"phase": "awaiting_approval"}) == A2ATaskState.COMPLETED
    assert a2a_state("suspended") == A2ATaskState.INPUT_REQUIRED


def test_deleted_push_config_not_used_by_running_task(monkeypatch):
    monkeypatch.setattr(th, "validate_push_url", lambda url: url)
    runner, agent, _ = _setup(monkeypatch, "running")
    asyncio.run(runner.set_push_notification_config(
        "t1", PushNotificationConfig(url="https://cb.example/hook"), user_id="u1"))
    assert runner._get_push_config("t1", agent._info) is not None
    # a NEW handler (one per request) deletes it
    other = A2ATaskHandler(_Container(agent))
    asyncio.run(other.delete_push_notification_config("t1", user_id="u1"))
    # the running task's handler no longer has a copy to push to
    assert runner._get_push_config("t1", agent._info) is None
    assert not hasattr(runner, "_push_configs")
