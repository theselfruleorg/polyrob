"""FIX 4 (goal-level half) — a tool-starved run must SAY it was tool-starved.

`load_tools_from_container` now records every requested tool_id that did not
register (tools/controller/tool_load_report.py). The dispatcher reads that back
off the run's controller so the gap lands where the owner and the next attempt
actually look: the goal's failure text, and the recorded result of a run that
"succeeded" without the capability it was granted (the `ship-software` stream
shipping a deliverable with no URL because `PUBLISH_ENABLED` is off).

Read duck-typed (`getattr(controller, "tool_gap_note", None)`) — the agents tier
must not import the tools tier (layering ratchet).
"""
import asyncio
from types import SimpleNamespace

from agents.task.goals.board import Goal, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.runtime.run_outcome import RunOutcome

GAP = ("[tool gap] requested but not registered: publish "
       "[gated:disabled-by-flag — PUBLISH_ENABLED is off]")


class _FakeBoard:
    def __init__(self):
        self.successes, self.failures, self.results = [], [], []
        self._status = "running"

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)
        self.results.append(result)
        self._status = "done"

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error))
        self._status = STATUS_READY
        return Goal(id=gid, user_id="u1", title="t", status=STATUS_READY)

    def get(self, gid):
        return Goal(id=gid, user_id="u1", title="t", status=self._status)

    def set_outcome(self, gid, outcome):
        return True

    def create_ask(self, **kw):
        return None


def _task_agent(note=GAP):
    controller = SimpleNamespace(tool_gap_note=lambda: note)
    orch = SimpleNamespace(controller=controller)
    return SimpleNamespace(deliver_self_wake=None, container=None,
                           get_orchestrator=lambda sid: orch)


def _patch_run(monkeypatch, **kw):
    base = dict(session_id="s-gap", status="Session completed successfully",
                steps=20, spend_usd=0.1, artifacts=[])
    base.update(kw)
    outcome = RunOutcome(**base)

    async def _fake(task_agent, *, user_id, request, autonomous=False, **_kw):
        # **_kw so a NEW kwarg on the real `run_task_to_outcome` (039 added
        # `goal_id`, to let an owner-queue ask name the goal it blocks) does
        # not fail 20 tests that never cared about it.
        return outcome

    monkeypatch.setattr("agents.task.goals.dispatcher._run_task_to_outcome", _fake)


def _goal():
    return Goal(id="g-gap", user_id="u1", title="ship the app",
                payload={"tools": ["filesystem", "publish"]})


def test_a_failed_run_names_the_tool_it_never_got(monkeypatch):
    board = _FakeBoard()
    disp = GoalDispatcher(board, _task_agent())
    _patch_run(monkeypatch, done_called=False)
    asyncio.run(disp._run_goal(_goal()))
    assert board.failures
    err = board.failures[0][1]
    assert "publish" in err and "PUBLISH_ENABLED" in err, err


def test_a_successful_run_still_records_the_gap(monkeypatch):
    """The deliverable-with-no-URL case: the run 'worked', the capability never existed."""
    board = _FakeBoard()
    disp = GoalDispatcher(board, _task_agent())
    _patch_run(monkeypatch, done_called=True)
    asyncio.run(disp._run_goal(_goal()))
    assert board.successes == ["g-gap"]
    assert "publish" in (board.results[0] or "")


def test_no_gap_means_no_note(monkeypatch):
    board = _FakeBoard()
    disp = GoalDispatcher(board, _task_agent(note=""))
    _patch_run(monkeypatch, done_called=True)
    asyncio.run(disp._run_goal(_goal()))
    assert "[tool gap]" not in (board.results[0] or "")


def test_a_controller_without_the_probe_is_harmless(monkeypatch):
    """Older/foreign controllers (and dead sessions) must never break a run."""
    board = _FakeBoard()
    agent = SimpleNamespace(deliver_self_wake=None, container=None,
                            get_orchestrator=lambda sid: SimpleNamespace())
    disp = GoalDispatcher(board, agent)
    _patch_run(monkeypatch, done_called=True)
    asyncio.run(disp._run_goal(_goal()))
    assert board.successes == ["g-gap"]
