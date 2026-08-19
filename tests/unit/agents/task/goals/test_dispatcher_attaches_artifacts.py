"""A goal run's artifacts get attributed to the goal — including a FAILED run.

The tools tier records (user_id, session_id) at write time; the dispatcher owns
the goal<->session mapping. Attribution must happen on EVERY exit path, not just
success: 46 goals died last week on "ran out of steps", and each had really
written evidence that the next round then could not find. Attributing a failed
run's output is what lets a retry resume instead of restart.

The goals here pin ``tools: ["filesystem"]``: the default goal toolset carries
``x402_invoice``, and the §6.2 metering gate fail-closes a money-enabled run
that has no database_manager — which would refuse before a session ever exists.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.goals.board import Goal, STATUS_BLOCKED, STATUS_READY
from agents.task.goals.dispatcher import GoalDispatcher
from core.artifacts import get_artifact_ledger
from tools.controller.types import ActionResult


class _Action:
    def __init__(self, name, params=None):
        self._d = {name: params or {}}

    def model_dump(self, exclude_unset=True):
        return dict(self._d)


class _Step:
    def __init__(self, actions, results):
        self.model_output = SimpleNamespace(action=list(actions))
        self.result = list(results)


class _Agent:
    def __init__(self, steps, *, n_steps=1):
        self.history = SimpleNamespace(history=list(steps))
        self._is_sub_agent = False
        self._last_result = steps[-1].result if steps else None
        self.state = SimpleNamespace(n_steps=n_steps)


class _TaskAgent:
    """Runs a session that produced a file; done() optional."""

    def __init__(self, *, done_text=None):
        if done_text is None:
            steps = [_Step([_Action("send_message", {"text": "still working"})],
                           [ActionResult(extracted_content="Message sent to user (non-blocking)")])]
        else:
            steps = [_Step([_Action("done", {"text": done_text})],
                           [ActionResult(is_done=True, extracted_content=done_text)])]
        self._orch = SimpleNamespace(agents={"main": _Agent(steps)},
                                     session_id="sess-art", usage_tracker=None)

    async def create_session(self, *, user_id, request):
        return {"id": "sess-art"}

    async def run_session(self, user_id, session_id):
        return "Session completed successfully"

    def get_orchestrator(self, session_id):
        return self._orch

    def _extract_chat_reply(self, session_id):
        return "Processing actions"

    deliver_self_wake = None


class _FakeBoard:
    def __init__(self):
        self.successes, self.failures, self.blocked, self.outcomes = [], [], [], []
        self.results = []
        self._status = "running"

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid); self.results.append(result); self._status = "done"

    def record_failure(self, gid, error=None, session_id=None):
        self.failures.append((gid, error)); self._status = STATUS_READY
        return Goal(id=gid, user_id="rob", title="t", status=STATUS_READY)

    def block_from_ready(self, gid, *, error):
        self.blocked.append((gid, error)); self._status = STATUS_BLOCKED; return True

    def get(self, gid):
        return Goal(id=gid, user_id="rob", title="t", status=self._status)

    def set_outcome(self, gid, outcome):
        self.outcomes.append((gid, outcome)); return True

    def create_ask(self, **kw):
        return None

    def stamp_block_kind(self, *a, **kw):
        return True


@pytest.fixture
def produced(tmp_path):
    """A file the run wrote, already recorded by the write choke point."""
    p = tmp_path / "round4-evidence.md"
    p.write_text("the decoded 402 challenge")
    get_artifact_ledger().record("rob", str(p), session_id="sess-art", kind="report")
    return p


def test_successful_run_attributes_its_artifacts(produced):
    disp = GoalDispatcher(_FakeBoard(), _TaskAgent(done_text="wrote the evidence file"))
    goal = Goal(id="g-success", user_id="rob", title="x402 Round 4",
                payload={"tools": ["filesystem"]})

    asyncio.run(disp._run_goal(goal))

    rows = get_artifact_ledger().list_for_goal("rob", "g-success")
    assert [r.path for r in rows] == [str(produced)]


def test_run_that_never_called_done_still_attributes_its_artifacts(produced):
    """Out of steps is not "produced nothing" — the evidence must survive the failure."""
    board = _FakeBoard()
    disp = GoalDispatcher(board, _TaskAgent(done_text=None))
    goal = Goal(id="g-outofsteps", user_id="rob", title="x402 Round 4",
                payload={"tools": ["filesystem"]})

    asyncio.run(disp._run_goal(goal))

    assert board.failures, "the run should be recorded as a failure"
    rows = get_artifact_ledger().list_for_goal("rob", "g-outofsteps")
    assert [r.path for r in rows] == [str(produced)], \
        "a failed run's artifacts must still be attributed, else a retry restarts blind"
