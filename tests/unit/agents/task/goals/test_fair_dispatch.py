"""Fair dispatch across objectives — per-objective counting and round-robin."""
import pytest

from agents.task.goals.board import GoalBoard


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_count_running_by_objective_groups_by_parent(tmp_path):
    board = GoalBoard(str(tmp_path / "goals.db"))
    a = board.create_objective(user_id="u1", title="stream A", force=True)
    b = board.create_objective(user_id="u1", title="stream B", force=True)
    g_a1 = board.create(user_id="u1", title="a1", parent_id=a.id, force=True)
    g_a2 = board.create(user_id="u1", title="a2", parent_id=a.id, force=True)
    g_b1 = board.create(user_id="u1", title="b1", parent_id=b.id, force=True)
    g_orphan = board.create(user_id="u1", title="no parent", force=True)

    assert board.count_running_by_objective() == {}

    board.claim(g_a1.id, "w1", ttl_seconds=900)
    board.claim(g_a2.id, "w1", ttl_seconds=900)
    board.claim(g_b1.id, "w1", ttl_seconds=900)
    board.claim(g_orphan.id, "w1", ttl_seconds=900)

    assert board.count_running_by_objective() == {a.id: 2, b.id: 1, "": 1}


def test_count_running_by_objective_ignores_expired_claims(tmp_path):
    now = [1000.0]
    board = GoalBoard(str(tmp_path / "goals.db"), clock=lambda: now[0])
    a = board.create_objective(user_id="u1", title="stream A", force=True)
    g = board.create(user_id="u1", title="a1", parent_id=a.id, force=True)
    board.claim(g.id, "w1", ttl_seconds=60)
    assert board.count_running_by_objective() == {a.id: 1}  # live claim counts
    now[0] += 120                                           # lease lapses
    assert board.count_running_by_objective() == {}         # expired claim does not


def test_ready_fair_round_robins_across_objectives(board):
    hot = board.create_objective(user_id="u1", title="hot stream", force=True)
    cold = board.create_objective(user_id="u1", title="cold stream", force=True)
    for i in range(3):
        board.create(user_id="u1", title=f"hot {i}", parent_id=hot.id, priority=9, force=True)
    board.create(user_id="u1", title="cold 0", parent_id=cold.id, priority=1, force=True)

    # Legacy order takes both slots from the hot stream.
    legacy = board.ready(limit=2)
    assert {g.parent_id for g in legacy} == {hot.id}

    picked = board.ready_fair(limit=2)
    assert {g.parent_id for g in picked} == {hot.id, cold.id}
    # Priority still decides who goes FIRST.
    assert picked[0].parent_id == hot.id


def test_ready_fair_fills_every_slot_when_one_stream_is_active(board):
    only = board.create_objective(user_id="u1", title="only stream", force=True)
    for i in range(4):
        board.create(user_id="u1", title=f"g{i}", parent_id=only.id, force=True)
    assert len(board.ready_fair(limit=3)) == 3


def test_ready_fair_respects_in_flight_and_cap(board):
    a = board.create_objective(user_id="u1", title="A", force=True)
    b = board.create_objective(user_id="u1", title="B", force=True)
    board.create(user_id="u1", title="a1", parent_id=a.id, priority=9, force=True)
    board.create(user_id="u1", title="b1", parent_id=b.id, priority=1, force=True)

    picked = board.ready_fair(limit=2, per_objective_cap=1, in_flight={a.id: 1})
    assert [g.parent_id for g in picked] == [b.id]


def test_ready_fair_buckets_parentless_goals_together(board):
    a = board.create_objective(user_id="u1", title="A", force=True)
    board.create(user_id="u1", title="orphan 1", priority=9, force=True)
    board.create(user_id="u1", title="orphan 2", priority=9, force=True)
    board.create(user_id="u1", title="a1", parent_id=a.id, priority=1, force=True)

    picked = board.ready_fair(limit=2)
    assert [g.parent_id for g in picked] == [None, a.id]


def test_ready_fair_zero_limit_returns_empty(board):
    a = board.create_objective(user_id="u1", title="A", force=True)
    board.create(user_id="u1", title="a1", parent_id=a.id, force=True)
    assert board.ready_fair(limit=0) == []


from agents.task.goals.dispatcher import GoalDispatcher


class _FakeAgent:
    """Minimal task-agent stand-in — mirrors tests/unit/agents/task/goals/test_goal_dispatcher.py."""

    def __init__(self):
        self.requests = []
        self.ran = []

    async def create_session(self, *, user_id, request):
        self.requests.append(request)
        return {"id": f"sess-{len(self.ran)}"}

    async def run_session(self, user_id, session_id):
        self.ran.append(session_id)
        return "done"

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        return True

    def get_orchestrator(self, session_id):
        return None


@pytest.mark.asyncio
async def test_dispatch_spreads_slots_across_objectives(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "2")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")
    monkeypatch.setenv("GOAL_FAIR_DISPATCH", "true")
    monkeypatch.setenv("GOAL_PLANNER_ENABLED", "false")

    hot = board.create_objective(user_id="u1", title="hot", force=True)
    cold = board.create_objective(user_id="u1", title="cold", force=True)
    for i in range(3):
        board.create(user_id="u1", title=f"hot {i}", parent_id=hot.id, priority=9, force=True)
    board.create(user_id="u1", title="cold 0", parent_id=cold.id, priority=1, force=True)

    d = GoalDispatcher(board, _FakeAgent())
    assert await d.dispatch_once() == 2
    claimed = [g for g in board.list(user_id="u1", limit=100)
               if g.kind == "goal" and g.status == "running"]
    assert {g.parent_id for g in claimed} == {hot.id, cold.id}


@pytest.mark.asyncio
async def test_dispatch_flag_off_restores_global_order(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "2")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")
    monkeypatch.setenv("GOAL_FAIR_DISPATCH", "false")
    monkeypatch.setenv("GOAL_PLANNER_ENABLED", "false")

    hot = board.create_objective(user_id="u1", title="hot", force=True)
    cold = board.create_objective(user_id="u1", title="cold", force=True)
    for i in range(3):
        board.create(user_id="u1", title=f"hot {i}", parent_id=hot.id, priority=9, force=True)
    board.create(user_id="u1", title="cold 0", parent_id=cold.id, priority=1, force=True)

    d = GoalDispatcher(board, _FakeAgent())
    assert await d.dispatch_once() == 2
    claimed = [g for g in board.list(user_id="u1", limit=100)
               if g.kind == "goal" and g.status == "running"]
    assert {g.parent_id for g in claimed} == {hot.id}


def test_ready_for_dispatch_falls_back_when_fair_path_raises(board, monkeypatch):
    monkeypatch.setenv("GOAL_FAIR_DISPATCH", "true")
    a = board.create_objective(user_id="u1", title="A", force=True)
    board.create(user_id="u1", title="a1", parent_id=a.id, force=True)

    def _boom(*_a, **_kw):
        raise RuntimeError("fairness exploded")

    monkeypatch.setattr(board, "ready_fair", _boom)
    d = GoalDispatcher(board, _FakeAgent())
    assert len(d._ready_for_dispatch(1)) == 1  # fell back to board.ready


def test_ready_for_dispatch_skips_the_in_flight_count_when_no_cap_is_set(board,
                                                                        monkeypatch):
    """`ready_fair` consults `in_flight` ONLY when a cap is set, so with the
    default `GOAL_PER_OBJECTIVE_CAP=0` the GROUP BY was running every tick for a
    value nothing read."""
    monkeypatch.setenv("GOAL_FAIR_DISPATCH", "true")
    monkeypatch.delenv("GOAL_PER_OBJECTIVE_CAP", raising=False)
    a = board.create_objective(user_id="u1", title="A", force=True)
    board.create(user_id="u1", title="a1", parent_id=a.id, force=True)

    calls = {"n": 0}
    real = board.count_running_by_objective

    def counted():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(board, "count_running_by_objective", counted)
    d = GoalDispatcher(board, _FakeAgent())
    assert len(d._ready_for_dispatch(1)) == 1
    assert calls["n"] == 0

    monkeypatch.setenv("GOAL_PER_OBJECTIVE_CAP", "1")
    assert len(d._ready_for_dispatch(1)) == 1
    assert calls["n"] == 1, "a set cap still needs the in-flight snapshot"


@pytest.mark.asyncio
async def test_maybe_plan_thinness_gate_reverts_with_goal_planner_scaling(board,
                                                                         monkeypatch):
    """`GOAL_PLANNER_MIN_READY` can no longer restore the legacy floor on its own
    — the scaled value is a `max()` of it. `GOAL_PLANNER_SCALING=false` must."""
    import asyncio

    monkeypatch.setenv("GOAL_PLANNER_ENABLED", "true")
    monkeypatch.setenv("GOAL_PLANNER_MIN_READY", "2")
    monkeypatch.setenv("GOAL_PLANNER_COOLDOWN_SEC", "0")
    monkeypatch.delenv("GOAL_PLANNER_READY_CEILING", raising=False)
    for i in range(8):
        board.create_objective(user_id="u1", title=f"stream {i}", force=True)
    for i in range(3):
        board.create(user_id="u1", title=f"work item {i}", force=True)

    d = GoalDispatcher(board, _FakeAgent())
    fired = []

    async def _fake_planner(user_id):
        fired.append(user_id)

    monkeypatch.setattr(d, "_run_planner", _fake_planner)

    # 3 ready goals: OVER the legacy flat floor of 2, UNDER the derived floor
    # of 8 (one per standing objective).
    monkeypatch.setenv("GOAL_PLANNER_SCALING", "false")
    await d._maybe_plan(headroom_after=0)
    await asyncio.sleep(0)
    assert fired == [], "the legacy flat GOAL_PLANNER_MIN_READY gate did not apply"

    monkeypatch.setenv("GOAL_PLANNER_SCALING", "true")
    await d._maybe_plan(headroom_after=0)
    await asyncio.sleep(0)
    assert fired == ["u1"], "the derived thinness gate did not fire"
