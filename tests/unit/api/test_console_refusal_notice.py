"""A console chat turn that halts on a model refusal names it in the chat.

The console sends a turn through ``api.task_http_api.send_user_message``, which runs
the session in the background; the console draws the session feed. Before this, a
refusal drew only the generic "This chat failed before the agent could answer."
Now the run writes ``run_outcome.session_turn_end_notice`` into the feed as a
``command_reply`` note, once, and only when the run halted on a refusal.
"""
import asyncio
import types

import pytest

import api.task_http_api as thp
from api.models import UserMessage as UserMessageRequest
from core.exceptions import REFUSAL_HALT_PREFIX

_REFUSAL = (f"{REFUSAL_HALT_PREFIX}the model declined this request (category: cyber). "
            "Nothing was retried on another model.")


@pytest.fixture(autouse=True)
def _paid_operator(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr("api.payment_verification.verify_payment_for_request",
                        AsyncMock(return_value=("admin_bypass", {})))
    monkeypatch.setattr(thp, "guard_remote", lambda agent, sid: None)


class _Feed:
    def __init__(self):
        self.events = []

    def add_to_feed(self, session_id, event_type, data, **_):
        self.events.append((event_type, data))

    def increment_task_phase(self, session_id):
        return 1


class _Orch:
    def __init__(self, error):
        step = types.SimpleNamespace(model_output=None,
                                     result=[types.SimpleNamespace(error=error)])
        main = types.SimpleNamespace(
            _is_sub_agent=False, history=types.SimpleNamespace(history=[step]),
            state=types.SimpleNamespace(stop_reason="error", max_steps=50, n_steps=1,
                                        exhausted_summary=None))
        self.agents = {"a": main}

    async def submit_user_message(self, agent, text, kind, metadata):
        pass


class _Agent:
    def __init__(self, status, run_result, error=_REFUSAL):
        self._session = {"id": "sess1", "user_id": "owner", "status": status, "task": "t",
                         "created_at": "2026-07-07T00:00:00", "config": {}, "metadata": {}}
        self._orch = _Orch(error)
        self._run_result = run_result
        self.active_sessions, self.user_sessions = {}, {}
        self.session_manager = _Feed()

    async def get_session_by_id(self, session_id):
        return dict(self._session, id=session_id)

    def get_orchestrator(self, session_id):
        return self._orch

    async def run_session(self, user_id, session_id):
        return self._run_result


def _notes(agent):
    return [d["text"] for t, d in agent.session_manager.events if t == "command_reply"]


async def _send(agent):
    req = types.SimpleNamespace(state=types.SimpleNamespace(user_id="owner"))
    await thp.send_user_message("sess1", UserMessageRequest(text="scan it"), req, agent=agent)
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "suspended"])
async def test_a_resumed_turn_names_the_refusal_in_the_feed(status):
    agent = _Agent(status, f"Session failed: {_REFUSAL}")
    await _send(agent)
    notes = _notes(agent)
    assert len(notes) == 1, notes
    assert notes[0].startswith(
        "⚠️ My turn ended early: the model declined this request (category: cyber)")


@pytest.mark.asyncio
async def test_a_first_turn_names_the_refusal_in_the_feed():
    agent = _Agent("created", f"Session failed: {_REFUSAL}")
    refused = agent._orch
    started = types.SimpleNamespace(agents={}, submit_user_message=refused.submit_user_message)
    ran = []
    real_run = agent.run_session

    async def _run(user_id, session_id):
        ran.append(session_id)
        return await real_run(user_id, session_id)
    # No agent before the run (the fresh-start branch runs it); the run's agent after.
    agent.get_orchestrator = lambda sid: refused if ran else started
    agent.run_session = _run
    await _send(agent)
    assert len(_notes(agent)) == 1


@pytest.mark.asyncio
async def test_a_turn_that_did_not_refuse_writes_no_note():
    agent = _Agent("completed", "Session failed: Unknown error", error="boom")
    await _send(agent)
    assert _notes(agent) == []
