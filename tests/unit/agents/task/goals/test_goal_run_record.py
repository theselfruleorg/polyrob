"""R3 of the 2026-10-13 memory/coherency review, goal half: the run record on
every exit path (B5, C-03), the goal's own previous runs (B6/B9), the END of a
long error (B9) and the upstream goals' results (B10)."""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.goals.board import Goal, GoalBoard
from agents.task.goals.context import build_goal_run_task
from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.runtime.run_outcome import RunOutcome
from tests.unit.agents.task.goals.test_dispatcher_run_outcome import _FakeBoard

TX = "0x" + "cd" * 32


@pytest.fixture
def records(monkeypatch):
    out = []

    async def _fin(**kw):
        out.append(kw)

    async def _no_prev(**kw):
        return []

    monkeypatch.setattr("modules.memory.episodic.finalize_episode", _fin)
    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", _no_prev)
    return out


def _patch_run(monkeypatch, fake):
    monkeypatch.setattr("agents.task.goals.dispatcher._run_task_to_outcome", fake)


def test_a_goal_that_bought_then_ran_out_of_steps_is_partial(monkeypatch, records):
    async def _fake(task_agent, *, user_id, request, **kw):
        return RunOutcome(session_id="s1", status="x", done_called=False,
                          user_messages=[f"bought, tx {TX}"],
                          artifacts=[{"kind": "defi_swap", "detail": f"ok {TX}"}], steps=20)
    _patch_run(monkeypatch, _fake)
    disp = GoalDispatcher(_FakeBoard(), SimpleNamespace(deliver_self_wake=None, container=None))
    asyncio.run(disp._run_goal(Goal(id="g1", user_id="u1", title="buy back")))
    rec = records[-1]
    assert rec["outcome"] == "partial" and rec["thread_key"] == "goal:g1"
    assert TX in rec["summary"] and rec["meta"]["tx"] == [TX]


def test_a_goal_cut_by_its_wall_clock_cap_leaves_a_record(monkeypatch, records):
    """B5: the cap raised TimeoutError with no text and ctx.session_id unset —
    no episode at all was written."""
    from core.autonomy_marker import mark_autonomous

    class _Agent:
        deliver_self_wake = None
        container = None

        async def create_session(self, *, user_id, request, session_id=None, **kw):
            return {"id": session_id}

    async def _hung(task_agent, *, user_id, request, goal_id=None, **kw):
        mark_autonomous(request["session_id"], goal_id)
        await asyncio.sleep(30)

    async def _resident(task_agent, session_id, status):
        return RunOutcome(session_id=session_id, done_called=False,
                          user_messages=[f"posted the thread, tx {TX}"])

    _patch_run(monkeypatch, _hung)
    monkeypatch.setattr("agents.task.runtime.run_outcome.build_run_outcome", _resident)
    monkeypatch.setattr("core.config_policy.AutonomyConfig.goal_max_run_seconds",
                        staticmethod(lambda: 0.05))
    disp = GoalDispatcher(_FakeBoard(), _Agent())
    asyncio.run(disp._run_goal(Goal(id="g2", user_id="u1", title="post")))
    rec = records[-1]
    assert rec["outcome"] == "partial"
    assert "wall-clock cap" in rec["summary"] and TX in rec["summary"]


def test_a_paused_goal_leaves_a_record(monkeypatch, records):
    from core.autonomy_marker import mark_autonomous
    started = asyncio.Event()

    class _Agent:
        deliver_self_wake = None
        container = None

        async def create_session(self, *, user_id, request, session_id=None, **kw):
            return {"id": session_id}

    async def _hung(task_agent, *, user_id, request, goal_id=None, **kw):
        mark_autonomous(request["session_id"], goal_id)
        started.set()
        await asyncio.sleep(30)

    async def _resident(task_agent, session_id, status):
        return RunOutcome(session_id=session_id, done_called=False)

    _patch_run(monkeypatch, _hung)
    monkeypatch.setattr("agents.task.runtime.run_outcome.build_run_outcome", _resident)

    async def main():
        disp = GoalDispatcher(_FakeBoard(), _Agent())
        t = asyncio.create_task(disp._run_goal(Goal(id="g3", user_id="u1", title="t")))
        await asyncio.wait_for(started.wait(), timeout=5)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(main())
    assert records[-1]["outcome"] == "cancelled"
    assert "paused by the owner" in records[-1]["summary"]


def test_the_goal_reads_its_own_previous_runs(monkeypatch, records):
    from modules.memory.provider import EpisodeRecord
    prev = EpisodeRecord(ts=1_760_000_000, user_id="u1", session_id="old", kind="goal",
                         outcome="failed", summary="FAILED: judge said the post is missing",
                         goal_id="g4", thread_key="goal:g4",
                         meta={"sent": ["drafted the thread"], "outputs": ["twitter_post: id 9"]})

    async def recall(**kw):
        return [prev] if kw.get("thread_key") == "goal:g4" else []

    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", recall)
    seen = {}

    async def _fake(task_agent, *, user_id, request, **kw):
        seen["task"] = request["task"]
        return RunOutcome(session_id="s", status="x", done_called=True, done_text="ok")

    _patch_run(monkeypatch, _fake)
    disp = GoalDispatcher(_FakeBoard(), SimpleNamespace(deliver_self_wake=None, container=None))
    asyncio.run(disp._run_goal(Goal(id="g4", user_id="u1", title="post the thread")))
    assert '<previous-runs rail="goal:g4">' in seen["task"]
    assert "twitter_post: id 9" in seen["task"] and "drafted the thread" in seen["task"]


def test_a_long_error_keeps_its_end(tmp_path):
    """B9: the tool-gap / acceptance detail at the END was cut by a head-500 clip."""
    board = GoalBoard(str(tmp_path / "g.db"))
    g = board.create(user_id="u1", title="write the report")
    board.claim(g.id, "w", ttl_seconds=60)
    err = "run ended without completing " + "x" * 900 + " [tool gap] defi_trade not registered"
    board.record_failure(g.id, error=err, claim_token=board.get(g.id).claim_token)
    task = build_goal_run_task(board.get(g.id))
    assert "[tool gap] defi_trade not registered" in task
    assert "run ended without completing" in task


def test_a_dependent_goal_sees_its_upstream_results(tmp_path):
    """B10: in "research, then write" the writer got the title only."""
    board = GoalBoard(str(tmp_path / "g.db"))
    research = board.create(user_id="u1", title="research the 3 launchpads")
    writer = board.create(user_id="u1", title="write the comparison post",
                          depends_on=[research.id])
    board.claim(research.id, "w", ttl_seconds=60)
    board.record_success(research.id, result="OUTCOME: workspace/launchpads.md — Clanker wins",
                         claim_token=board.get(research.id).claim_token)
    disp = GoalDispatcher(board, SimpleNamespace(deliver_self_wake=None, container=None))
    up = disp._upstream_results(board.get(writer.id))
    task = build_goal_run_task(board.get(writer.id), upstream=up)
    assert "UPSTREAM GOALS" in task and "Clanker wins" in task
    assert "research the 3 launchpads" in task
