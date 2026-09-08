"""031 T4: dispatch consults the pause record and holds in-flight runs once."""
import asyncio

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher


@pytest.fixture
def home(tmp_path, monkeypatch):
    for k in ("AUTONOMY_HALT", "TREASURY_ENTRY_PAUSE", "STREAM_SEEDING_PAUSE", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("GOALS_ENABLED", "true")
    return tmp_path


@pytest.mark.asyncio
async def test_paused_dispatch_holds_inflight_once(home):
    from core import autonomy_control as ac
    board = GoalBoard(str(home / "goals.db"))
    g = board.create(user_id="rob", title="long")
    d = GoalDispatcher(board, task_agent=object())
    assert board.claim(g.id, d._worker, ttl_seconds=60)
    started = asyncio.Event()

    async def fake_run():
        started.set()
        await asyncio.sleep(60)

    t = asyncio.create_task(fake_run())
    d._inflight.add(t)
    t.add_done_callback(d._inflight.discard)
    await started.wait()
    ac.pause(str(home), scopes=("all",), via="test")
    n = await d.dispatch_once()
    assert n == 0 and t.done() and t.cancelled()
    assert board.get(g.id).status == "ready"
    assert board.get(g.id).consecutive_failures == 0
    assert d._paused_seen is True
    # a second paused tick is a plain no-op (no re-hold, no re-log)
    assert await d.dispatch_once() == 0
    ac.resume(str(home))
    d._paused_seen = True
    await d.dispatch_once()
    assert d._paused_seen is False


@pytest.mark.asyncio
async def test_planner_scope_blocks_plan_not_dispatch(home, monkeypatch):
    from core import autonomy_control as ac
    board = GoalBoard(str(home / "goals.db"))
    d = GoalDispatcher(board, task_agent=object())
    ac.pause(str(home), scopes=("planner",), via="test")
    called = []

    async def _planner(uid):
        called.append(uid)

    monkeypatch.setattr(d, "_run_planner", _planner)
    monkeypatch.setattr(d, "_active_objective_owners",
                        lambda: [type("O", (), {"user_id": "rob"})()])
    monkeypatch.setenv("GOAL_PLANNER_ENABLED", "true")
    await d._maybe_plan(headroom_after=1)
    assert called == []
    assert ac.allows("dispatch", str(home)).allowed is True
