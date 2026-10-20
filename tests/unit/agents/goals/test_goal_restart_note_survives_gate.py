"""B13 (2026-10-13 review): the restart note is cleared only after every gate
that can stop the run before it starts — a gate that stops the run must leave
the note for the run that really happens."""
import pytest

from agents.task.goals.board import Goal
from tests.unit.agents.goals.test_goal_resume_same_session import (
    _CapturingAgent, _NoteBoard,
)


@pytest.mark.asyncio
async def test_a_gate_that_stops_the_run_keeps_the_restart_note(monkeypatch):
    from agents.task.goals import dispatcher as d
    import agents.task.runtime.metering_gate as mg
    monkeypatch.setattr(mg, "unmetered_money_gate", lambda *a, **k: "unmetered money run refused")
    goal = Goal(id="g-note", user_id="rob", title="Round 15", session_id="sess-15",
                payload={"tools": ["filesystem"], "resume_note": "pre-empted at step 7"})
    board = _NoteBoard(goal)
    agent = _CapturingAgent()
    disp = d.GoalDispatcher(board, agent)
    await disp._run_goal(goal)
    assert board.failures, "the gate stopped the run"
    assert agent.ran == [] and agent.requests == []
    assert (goal.payload or {}).get("resume_note") == "pre-empted at step 7"


@pytest.mark.asyncio
async def test_a_run_that_starts_consumes_the_note(monkeypatch):
    from agents.task.goals import dispatcher as d
    monkeypatch.delenv("GOAL_RESUME_SAME_SESSION", raising=False)
    goal = Goal(id="g-note2", user_id="rob", title="Round 15", session_id="sess-15",
                payload={"tools": ["filesystem"], "resume_note": "pre-empted at step 7"})
    disp = d.GoalDispatcher(_NoteBoard(goal), _CapturingAgent())
    await disp._run_goal(goal)
    assert not (goal.payload or {}).get("resume_note")
