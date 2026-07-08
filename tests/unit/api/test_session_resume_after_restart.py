"""Crash-mid-turn recovery: a session interrupted when the process died is rewritten
running->suspended at startup, so it enters the resumable block. In a FRESH process its
orchestrator is absent, so STEP 6 never queues the inbound message. The fix delivers the
message via ensure_session_and_deliver (recreate-then-queue) so the resume actually
processes it — instead of the message being silently dropped. A resident session must NOT
be double-delivered (STEP 6 already queued it).
"""
import pytest

import api.task_http_api as thp
from api.models import UserMessage as UserMessageRequest


class _FakeState:
    def __init__(self, user_id):
        self.user_id = user_id


class _FakeRequest:
    def __init__(self, user_id):
        self.state = _FakeState(user_id)


class _FakeSessionManager:
    def add_to_feed(self, *a, **k):
        pass

    def increment_task_phase(self, session_id):
        return 1


class _Orchestrator:
    def __init__(self):
        self.agents = {"a": object()}
        self.submitted = []

    async def submit_user_message(self, agent, text, kind, metadata):
        self.submitted.append((text, kind))


class _FakeAgent:
    def __init__(self, *, status="suspended", orchestrator=None):
        self._session = {
            "id": "s1", "user_id": "owner", "status": status,
            "task": "t", "created_at": "2026-07-07T00:00:00", "config": {}, "metadata": {},
        }
        self._orch = orchestrator
        self.active_sessions = {}
        self.user_sessions = {}
        self.session_manager = _FakeSessionManager()
        self.run_calls = []
        self.delivered = []  # (user_id, session_id, text) via ensure_session_and_deliver

    async def get_session_by_id(self, session_id):
        s = dict(self._session)
        s["id"] = session_id
        return s

    def get_orchestrator(self, session_id):
        return self._orch

    async def ensure_session_and_deliver(self, user_id, session_id, text, *,
                                         kind="comment", metadata=None):
        self.delivered.append((user_id, session_id, text))
        return "delivered"

    async def run_session(self, user_id, session_id):
        self.run_calls.append((user_id, session_id))
        return "ok"


@pytest.mark.asyncio
async def test_suspended_session_delivers_message_in_fresh_process(monkeypatch):
    # The realistic post-restart status is "suspended" (startup sweep). Fresh process:
    # orchestrator absent -> recreate + DELIVER the message, then run.
    monkeypatch.setattr(thp, "guard_remote", lambda agent, sid: None)
    agent = _FakeAgent(status="suspended", orchestrator=None)
    req = _FakeRequest("owner")
    await thp.send_user_message("s1", UserMessageRequest(text="continue please"), req, agent=agent)
    assert agent.delivered and agent.delivered[0][2] == "continue please"  # message delivered
    assert len(agent.run_calls) == 1 and agent.run_calls[0][0] == "owner"   # then run once


@pytest.mark.asyncio
async def test_completed_session_delivers_message_in_fresh_process(monkeypatch):
    # The completed-resume path had the SAME orchestrator-None drop; the fix delivers
    # there too (a completed session with no pending input otherwise short-circuits).
    monkeypatch.setattr(thp, "guard_remote", lambda agent, sid: None)
    agent = _FakeAgent(status="completed", orchestrator=None)
    req = _FakeRequest("owner")
    await thp.send_user_message("s1", UserMessageRequest(text="one more thing"), req, agent=agent)
    assert agent.delivered and agent.delivered[0][2] == "one more thing"
    assert len(agent.run_calls) == 1


@pytest.mark.asyncio
async def test_resident_session_uses_step6_not_ensure_deliver(monkeypatch):
    # A resident (in-process) session: STEP 6 queues the message on the live
    # orchestrator; ensure_session_and_deliver must NOT be called (no double delivery),
    # and the session still runs once to process the queued message.
    monkeypatch.setattr(thp, "guard_remote", lambda agent, sid: None)
    orch = _Orchestrator()
    agent = _FakeAgent(status="suspended", orchestrator=orch)
    req = _FakeRequest("owner")
    await thp.send_user_message("s1", UserMessageRequest(text="mid-run guidance"), req, agent=agent)
    assert not agent.delivered                       # STEP 6 handled it, not ensure_deliver
    assert orch.submitted and orch.submitted[0][0] == "mid-run guidance"
    assert len(agent.run_calls) == 1
