"""dispatch_once runs no goal-board call on the event-loop thread (proposal 125 S18b).

A board call is a synchronous SQLite write or read (execute_retry can sleep for
seconds on a contended goals.db). The static ratchet is
``test_no_blocking_io_on_event_loop.py::test_store_calls_are_off_the_loop``; this
runtime check proves it on a real tick, the helpers included.
"""
import inspect
import threading

import pytest

from modules.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher


def _recording_board(path, seen):
    class _Recording(GoalBoard):
        pass

    for name, fn in inspect.getmembers(GoalBoard, inspect.isfunction):
        if name.startswith("__"):
            continue

        def _wrap(fn=fn, name=name):
            def _m(self, *a, **k):
                seen.append((name, threading.get_ident()))
                return fn(self, *a, **k)
            return _m
        setattr(_Recording, name, _wrap())
    board = _Recording.__new__(_Recording)
    GoalBoard.__init__(board, path)
    seen.clear()
    return board


@pytest.fixture
def home(tmp_path, monkeypatch):
    for k in ("AUTONOMY_HALT", "TREASURY_ENTRY_PAUSE", "STREAM_SEEDING_PAUSE", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("GOALS_ENABLED", "true")
    return tmp_path


@pytest.mark.asyncio
async def test_dispatch_once_runs_no_board_call_on_the_loop_thread(home, monkeypatch):
    seen = []
    board = _recording_board(str(home / "goals.db"), seen)
    board.create(user_id="alice", title="write the report")
    seen.clear()
    d = GoalDispatcher(board, task_agent=object(), lock_path=str(home / "t.lock"))
    monkeypatch.setattr(GoalDispatcher, "dispatch_blocked_by_providers",
                        staticmethod(lambda: False))
    started = []

    async def _fake_run(goal):
        started.append(goal.id)
    monkeypatch.setattr(d, "_run_goal", _fake_run)

    loop_thread = threading.get_ident()
    n = await d.dispatch_once()
    for t in list(d._inflight):
        await t
    assert seen, "the tick made no board call"
    on_loop = sorted({name for name, tid in seen if tid == loop_thread})
    assert on_loop == [], f"board calls on the event-loop thread: {on_loop}"
    assert n == 1 and started
