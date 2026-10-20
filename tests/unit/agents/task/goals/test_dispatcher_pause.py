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
async def test_retry_after_pause_never_runs_two_copies(home):
    """/goal pause on a running goal, then /goal retry: the re-claim must stop
    the old run (it lost its claim) instead of running beside it until the
    next heartbeat."""
    from core.goal_owner_verbs import pause_goal, retry_goal
    board = GoalBoard(str(home / "goals.db"))
    g = board.create(user_id="rob", title="long")
    d = GoalDispatcher(board, task_agent=object())
    assert board.claim(g.id, d._worker, ttl_seconds=60)

    async def hang(*_a, **_k):
        await asyncio.sleep(3600)

    old = asyncio.create_task(hang())
    d._goal_tasks[g.id] = old
    assert pause_goal(board, user_id="rob", goal=board.get(g.id)).ok
    assert retry_goal(board, user_id="rob", goal=board.get(g.id)).ok
    d._run_goal = hang
    assert await d.dispatch_once() == 1
    await asyncio.sleep(0)
    assert old.cancelled()
    new = d._goal_tasks[g.id]
    assert new is not old and not new.done()
    new.cancel()
