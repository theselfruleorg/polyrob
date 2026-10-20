"""083 WS-4 — one operator step cap for every autonomous run.

``AUTONOMOUS_MAX_STEPS`` (default 150) is the step cap of a goal, cron,
delegation and worker run. A stored ``max_steps`` (goals.db / cron.db payload,
a worker's ``limits.max_steps`` pin) is inert. A room-service run keeps its
short 8. A credit-billed tenant's autonomous run keeps the billed ceiling (50).
"""
from dataclasses import dataclass

import pytest

from agents.task.runtime.run_outcome import RunOutcome
from core.config_policy import AutonomyConfig


def test_the_cap_defaults_to_150(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_MAX_STEPS", raising=False)
    assert AutonomyConfig.autonomous_max_steps() == 150


@pytest.mark.parametrize("raw,expected", [("40", 40), ("0", 150), ("-3", 150),
                                          ("lots", 150)])
def test_the_cap_reads_the_operator_flag(monkeypatch, raw, expected):
    monkeypatch.setenv("AUTONOMOUS_MAX_STEPS", raw)
    assert AutonomyConfig.autonomous_max_steps() == expected


def test_the_retired_goal_default_names_its_replacement():
    from core.config_policy.retired_flags import RETIRED_FLAGS
    assert RETIRED_FLAGS["GOAL_DEFAULT_MAX_STEPS"] == "AUTONOMOUS_MAX_STEPS"
    assert not hasattr(AutonomyConfig, "goal_default_max_steps")


@pytest.mark.asyncio
async def test_a_goal_run_ignores_a_stored_step_budget(tmp_path, monkeypatch):
    from agents.task.goals import dispatcher as disp_mod
    from agents.task.goals.board import GoalBoard
    from agents.task.goals.dispatcher import GoalDispatcher
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("AUTONOMOUS_MAX_STEPS", "77")
    board = GoalBoard(str(tmp_path / "goals.db"))
    g = board.create(user_id="u1", title="record row", payload={"max_steps": 10})
    g = board.claim(g.id, "w", ttl_seconds=900)
    seen = {}

    async def _capture(task_agent, *, request, **kw):
        seen.update(request)
        return RunOutcome(session_id="s1", status="ok", done_called=True,
                          done_text="done", user_messages=[])

    monkeypatch.setattr(disp_mod, "_run_task_to_outcome", _capture)

    class _Agent:
        pass

    await GoalDispatcher(board, _Agent())._run_goal(g)
    assert seen.get("max_steps") == 77, seen


@pytest.mark.asyncio
async def test_a_cron_run_ignores_a_stored_step_budget(tmp_path, monkeypatch):
    from cron.jobs import CronJob
    from cron.runner import make_agent_runner
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("AUTONOMOUS_MAX_STEPS", "66")
    seen = {}

    async def _capture(*a, request, **k):
        seen.update(request)
        return RunOutcome(session_id="s1", status="ok", done_called=True,
                          done_text="nothing new", user_messages=[])

    monkeypatch.setattr("cron.runner._run_task_to_outcome", _capture)
    job = CronJob(id="j1", task="watch the pool", schedule_spec="30m", user_id="u1",
                  next_run_at=None, one_shot=True, skip_memory=True,
                  max_duration_seconds=180, payload={"max_steps": 20}, created_at=None)

    class _Agent:
        async def create_session(self, *, user_id, request):
            return {"id": "s1"}

    await make_agent_runner(_Agent(), data_dir=str(tmp_path))(job)
    assert seen.get("max_steps") == 66, seen


def test_a_room_run_keeps_its_short_budget_under_the_cap(monkeypatch):
    from agents.task.goals.group_service import room_run_max_steps
    monkeypatch.delenv("AUTONOMOUS_MAX_STEPS", raising=False)
    assert room_run_max_steps() == 8
    monkeypatch.setenv("AUTONOMOUS_MAX_STEPS", "5")
    assert room_run_max_steps() == 5


def test_a_worker_pin_is_inert():
    from agents.task.agent.profile_store import WorkerSpec
    from agents.task.config import AgentProfileModel
    model = AgentProfileModel(id="researcher", name="researcher",
                              limits={"max_steps": 9, "pinned": True})
    assert not hasattr(WorkerSpec(model), "max_steps")


@dataclass
class _Req:
    max_steps: int = 50


def test_a_billed_autonomous_run_keeps_the_billed_ceiling(monkeypatch):
    from agents.task import billed_request as br
    import agents.task.session_class as sc
    monkeypatch.setattr(sc, "is_autonomous_session", lambda sid: sid == "auto")
    req = _Req(max_steps=150)
    br.clamp_autonomous_billed_steps(req, "auto")
    assert req.max_steps == 50
    interactive = _Req(max_steps=150)
    br.clamp_autonomous_billed_steps(interactive, "chat")
    assert interactive.max_steps == 150, "an interactive request is refused, never clamped"
