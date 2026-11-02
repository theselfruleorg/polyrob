"""A sub-agent (the background skill reviewer) never becomes the session (prod 10-19).

Owner message 1878 on 2026-10-19 was answered by the background REVIEWER: the
session was recreated after a restart and acted out the reviewer's run
(skill_manage x2, done with the reviewer's bookkeeping). A sub-agent carries the
PARENT's session id, so two writes landed on the parent's files:

- the end of every ``run_session`` saved the message history of EVERY agent in the
  orchestrator to the session's ``message_history.json`` — the reviewer, iterated
  after the executor, overwrote the executor's history;
- the step auto-save wrote the reviewer's ``AgentState`` to ``agent_state.json``,
  and every new agent (a sub-agent too) restored from that file.
"""
import asyncio
from types import SimpleNamespace

from agents.task.agent.agent_state import AgentState

S = "sess-owner"


class _MM:
    def __init__(self, name, saves):
        self.name, self.saves = name, saves

    def save_to_disk(self, session_id, user_id=None):
        self.saves.append((self.name, session_id))


def test_run_session_saves_only_the_main_agents_history(monkeypatch):
    from agents.task_agent_lite import TaskAgent

    saves = []
    executor = SimpleNamespace(agent_id=f"executor_{S}", message_manager=_MM("executor", saves),
                               _is_sub_agent=False, reset_for_continuation=lambda: None)
    reviewer = SimpleNamespace(agent_id="bg_review_1", message_manager=_MM("reviewer", saves),
                               _is_sub_agent=True)

    async def _execute_session(**kw):
        return {executor.agent_id: {"status": "completed"}}

    async def _cleanup(**kw):
        return None

    orch = SimpleNamespace(agents={executor.agent_id: executor, reviewer.agent_id: reviewer},
                           user_id="alice", execute_session=_execute_session, cleanup=_cleanup)
    info = {"user_id": "alice", "status": "created",
            "request": {"task": "hi", "max_steps": 50}}

    ta = TaskAgent.__new__(TaskAgent)
    ta.session_manager = SimpleNamespace(
        get_session_info=lambda sid: info,
        try_transition_status=lambda sid, a, b: True,
        update_session_status=lambda sid, st: None)
    ta._registry = SimpleNamespace(get=lambda sid: orch, count=lambda: 1)
    ta._session_last_activity = {}
    ta.telemetry = None
    out = asyncio.run(ta._run_session_impl("alice", S))
    assert out == "Session completed successfully"
    assert saves == [("executor", S)]          # the reviewer never writes the session


def test_a_sub_agent_never_auto_saves_the_session_agent_state(tmp_path, monkeypatch):
    from agents.task.agent.core import step as step_mod

    state_file = tmp_path / "agent_state.json"
    monkeypatch.setattr("agents.task.path.pm", lambda: SimpleNamespace(
        create_file_path=lambda **kw: state_file))
    saved = []
    sub = SimpleNamespace(
        _is_sub_agent=True, session_id=S, user_id="alice", tool_call_tracker=None,
        state=SimpleNamespace(n_steps=5, save_to_file=lambda p: saved.append(p) or True),
        message_manager=None, logger=SimpleNamespace(debug=lambda *a, **k: None,
                                                      error=lambda *a, **k: None,
                                                      warning=lambda *a, **k: None))

    async def _telemetry(*a, **k):
        return []
    sub._emit_step_telemetry = _telemetry
    asyncio.run(step_mod.StepMixin._finalize_step(sub, None, None, None))
    assert saved == []


def test_a_sub_agent_never_restores_the_session_agent_state(tmp_path):
    """The reviewer's state in the file must not seed a NEW sub-agent either."""
    from agents.task.agent.core.construction import should_restore_agent_state
    assert should_restore_agent_state(is_sub_agent=True, session_id=S, orchestrator=object()) is False
    assert should_restore_agent_state(is_sub_agent=False, session_id=S, orchestrator=object()) is True
    assert should_restore_agent_state(is_sub_agent=False, session_id="", orchestrator=object()) is False
    assert AgentState  # the restored type is unchanged
