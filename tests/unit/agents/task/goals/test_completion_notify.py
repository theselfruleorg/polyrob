"""Communication fix (2026-07-08): a completed background goal must NOTIFY THE OWNER
even when self-wake re-entry is OFF.

The owner-completion push used to live ONLY inside `_self_wake`, gated by the
posture-off `GOAL_SELF_WAKE_ENABLED`, so on the server completed goals told no one.
`_notify_owner_done` is now decoupled and default-ON (`GOAL_NOTIFY_ON_DONE`).
"""
import asyncio

from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.goals.board import Goal
from agents.task.constants import AutonomyConfig


class _Board:
    def __init__(self):
        self.successes = []

    def record_success(self, gid, session_id=None, result=None):
        self.successes.append(gid)

    def record_failure(self, gid, error=None, session_id=None):
        pass

    def get(self, gid):
        return None  # owner didn't cancel/pause → proceed

    def set_outcome(self, gid, outcome):
        pass


class _DoneAgent:
    container = None
    deliver_self_wake = None  # self-wake capability NOT wired (server default)

    async def create_session(self, *, user_id, request):
        return {"id": "s1"}

    async def run_session(self, user_id, session_id):
        return "Done: built the thing, all tests pass."


def _run(board, agent, goal, monkeypatch, notify=True, self_wake=False):
    # Patch the LIVE module attribute, not the class imported at collection
    # time: the dispatcher imports AutonomyConfig lazily at call time, and an
    # earlier test's `importlib.reload(agents.task.constants)` (e.g. the
    # project-context suite) rebinds the class inside the module — patching
    # the stale collection-time reference silently misses (order-dependent
    # flake, 2026-07-13; same landmine test_local_profile.py documents).
    import agents.task.constants as cts
    monkeypatch.setattr(cts.AutonomyConfig, "goal_self_wake_enabled", lambda: self_wake)
    monkeypatch.setattr(cts.AutonomyConfig, "goal_notify_on_done", lambda: notify)
    pushed = []
    import core.self_evolution as se

    async def _fake_push(container, text):
        pushed.append(text)
        return True

    monkeypatch.setattr(se, "push_owner_message", _fake_push)
    asyncio.run(GoalDispatcher(board, agent)._run_goal(goal))
    return pushed


def test_completion_notifies_owner_even_when_self_wake_off(monkeypatch):
    board = _Board()
    pushed = _run(board, _DoneAgent(), Goal(id="g1", user_id="u1", title="build the thing"),
                  monkeypatch, notify=True, self_wake=False)
    assert board.successes == ["g1"]
    assert pushed, "owner MUST be notified of completion even with self-wake OFF"
    # §4.3: a fake agent can't be evidence-verified, so the honest label is
    # "done (unverified)" — the push still fires, no ✅ on an unchecked claim.
    assert "build the thing" in pushed[0]
    assert ("completed" in pushed[0].lower()) or ("unverified" in pushed[0].lower())


def test_notify_on_done_defaults_true():
    assert AutonomyConfig.goal_notify_on_done() is True


def test_disabled_flag_silences_the_push(monkeypatch):
    pushed = _run(_Board(), _DoneAgent(), Goal(id="g3", user_id="u1", title="x"),
                  monkeypatch, notify=False, self_wake=False)
    assert not pushed, "GOAL_NOTIFY_ON_DONE=false must silence the completion push"
