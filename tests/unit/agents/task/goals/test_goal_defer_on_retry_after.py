"""A goal run cut short by a rate guard is deferred, not failed.

Prod 2026-10-14 18:13Z: goal 0de8cdf8d4ae was claimed 2 min after the previous
X post; ``twitter_post`` refused with ``retry_after_sec=3364`` (the 1 h autonomous
post cooldown), the run ended as a refusal, the goal was re-claimed a minute later
into the same refusal, and it gave up 2/2 inside ten minutes — the owner got
"I'm blocked" for a timer. The guard names when to come back; the board now
honours it: no failure, ``ready`` again, and not claimable before that time.
"""
import types

import pytest

from agents.task.goals.board import GoalBoard, STATUS_READY


@pytest.fixture
def board(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    b._now = lambda: 1_000_000.0
    return b


def _running(board):
    g = board.create(user_id="rob", title="X growth cycle")
    return board.claim(g.id, "w1", ttl_seconds=600)


def test_defer_requeues_without_a_failure_and_holds_until_not_before(board):
    g = _running(board)

    out = board.defer_run(g.id, retry_after_sec=3364, claim_token=g.claim_token,
                          reason="post cooldown")

    assert out is not None and out.status == STATUS_READY
    assert out.consecutive_failures == 0
    assert out.payload["not_before"] == pytest.approx(1_000_000.0 + 3364)
    assert board.ready(limit=10) == []
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "deferred" in kinds and "failed" not in kinds

    board._now = lambda: 1_000_000.0 + 3365
    assert [x.id for x in board.ready(limit=10)] == [g.id]


def test_a_held_goal_does_not_hide_the_next_one(board):
    held = _running(board)
    board.defer_run(held.id, retry_after_sec=600, claim_token=held.claim_token)
    other = board.create(user_id="rob", title="other")

    assert [x.id for x in board.ready(limit=1)] == [other.id]


def test_deferral_is_capped_so_a_guard_cannot_hold_a_goal_forever(board):
    g = board.create(user_id="rob", title="loops on a guard")
    for _ in range(GoalBoard.MAX_RUN_DEFERRALS):
        c = board.claim(g.id, "w1", ttl_seconds=600)
        assert board.defer_run(g.id, retry_after_sec=1, claim_token=c.claim_token) is not None
        board._now = lambda: 2_000_000.0
    c = board.claim(g.id, "w1", ttl_seconds=600)
    assert board.defer_run(g.id, retry_after_sec=1, claim_token=c.claim_token) is None


def test_a_stale_claim_cannot_defer(board):
    g = _running(board)
    assert board.defer_run(g.id, retry_after_sec=60, claim_token="not-mine") is None


def test_run_outcome_reads_the_largest_retry_after():
    from agents.task.runtime.run_outcome import retry_after_from

    def step(*texts):
        return types.SimpleNamespace(
            result=[types.SimpleNamespace(extracted_content=t, error=None) for t in texts])

    agent = types.SimpleNamespace(
        _is_sub_agent=False,
        history=types.SimpleNamespace(history=[
            step("ok"),
            step("ERROR: Twitter write 'twitter_post' blocked … retry_after_sec=3364. If …"),
            step("rate limited, retry_after_sec=120"),
        ]))
    orch = types.SimpleNamespace(agents={"a": agent})

    assert retry_after_from(orch) == 3364
    assert retry_after_from(types.SimpleNamespace(agents={})) is None


def test_dispatcher_defers_a_refused_run_that_names_retry_after(tmp_path):
    """End to end through ``_run_goal``: the refused run's ledger carries the
    cooldown's retry_after, so the board defers instead of failing."""
    import asyncio
    from agents.task.goals.dispatcher import GoalDispatcher

    board = GoalBoard(str(tmp_path / "g.db"))
    g = board.create(user_id="u1", title="X post cycle")
    claimed = board.claim(g.id, "w1", ttl_seconds=600)

    refused = types.SimpleNamespace(result=[types.SimpleNamespace(
        extracted_content="ERROR: Twitter write 'twitter_post' blocked … retry_after_sec=900.",
        error=None)])
    orch = types.SimpleNamespace(agents={"a": types.SimpleNamespace(
        _is_sub_agent=False, history=types.SimpleNamespace(history=[refused]))})

    class _Agent:
        deliver_self_wake = None

        async def create_session(self, *, user_id, request):
            return {"id": "s1"}

        async def run_session(self, user_id, session_id):
            return "No active session found"

        def get_orchestrator(self, sid):
            return orch

    asyncio.run(GoalDispatcher(board, _Agent())._run_goal(claimed))

    row = board.get(g.id)
    assert row.status == STATUS_READY
    assert row.consecutive_failures == 0
    assert "deferred" in [e["kind"] for e in board.events(g.id)]


def test_a_budget_halt_is_a_failure_even_when_a_guard_named_retry_after(tmp_path):
    """A RUN_BUDGET_USD halt spent the run's budget: deferring it would re-run the
    goal (and spend again) up to MAX_RUN_DEFERRALS times with no failure counted.
    A rate guard's retry_after elsewhere in the same run must not turn it into a timer."""
    import asyncio
    from agents.task.goals.dispatcher import GoalDispatcher

    board = GoalBoard(str(tmp_path / "g.db"))
    g = board.create(user_id="u1", title="X post cycle")
    claimed = board.claim(g.id, "w1", ttl_seconds=600)

    refused = types.SimpleNamespace(result=[types.SimpleNamespace(
        extracted_content="ERROR: Twitter write 'twitter_post' blocked … retry_after_sec=900.",
        error=None)])
    orch = types.SimpleNamespace(agents={"a": types.SimpleNamespace(
        _is_sub_agent=False, history=types.SimpleNamespace(history=[refused]))})

    class _Agent:
        deliver_self_wake = None

        async def create_session(self, *, user_id, request):
            return {"id": "s1"}

        async def run_session(self, user_id, session_id):
            return ("Session failed: run_budget_exhausted: session provider spend "
                    "$0.5000 reached the $0.50 cap")

        def get_orchestrator(self, sid):
            return orch

    asyncio.run(GoalDispatcher(board, _Agent())._run_goal(claimed))

    row = board.get(g.id)
    assert row.consecutive_failures == 1
    assert "run_budget_exhausted" in (row.last_failure_error or "")
    assert "deferred" not in [e["kind"] for e in board.events(g.id)]


def test_the_deferral_cap_counts_in_a_row_not_lifetime(board):
    """A recorded failure (or success) ends the row: the next guard gets a
    fresh MAX_RUN_DEFERRALS, so a long-lived goal is not failed on its 4th
    deferral ever."""
    g = board.create(user_id="rob", title="X growth cycle", max_retries=5)
    for _ in range(board.MAX_RUN_DEFERRALS):
        c = board.claim(g.id, "w1", ttl_seconds=600)
        assert board.defer_run(g.id, retry_after_sec=1, claim_token=c.claim_token)
    c = board.claim(g.id, "w1", ttl_seconds=600)
    assert board.defer_run(g.id, retry_after_sec=1, claim_token=c.claim_token) is None
    board.record_failure(g.id, error="cooldown", claim_token=c.claim_token)
    assert "run_deferrals" not in board.get(g.id).payload
    c = board.claim(g.id, "w1", ttl_seconds=600)
    assert board.defer_run(g.id, retry_after_sec=1, claim_token=c.claim_token)
