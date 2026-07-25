"""Board DAG core (T2.1 Task 1): dependency edges, waiting status,
create(depends_on=), add_dependencies, deps_satisfied.

T2.1 Task 2 (completion sweep + failure cascade) extends this file:
``record_success``'s dependents sweep, the ``dep_failed`` inverse cascade
(cancel / record_failure breaker trip / reclaim_stale breaker trip), the
owner ``unblock`` override, and the creation-time terminal-bad-dep closure.
"""
import pytest

from agents.task.goals.board import (
    GoalBoard, STATUS_BLOCKED, STATUS_CANCELLED, STATUS_DONE, STATUS_READY,
    STATUS_WAITING,
)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _finish(board, goal):
    """Drive a goal through claim -> record_success so it lands 'done'."""
    board.claim(goal.id, "w", ttl_seconds=900)
    board.record_success(goal.id, session_id="s", result="ok")
    return board.get(goal.id)


# --- create(depends_on=) ----------------------------------------------------

def test_create_no_deps_is_legacy_ready(board):
    g = board.create(user_id="u1", title="solo")
    assert g.status == STATUS_READY
    assert board.dependencies(g.id) == []
    kinds = [e["kind"] for e in board.events(g.id)]
    assert kinds == ["created"]  # byte-identical: no waiting_on_deps noise


def test_create_with_empty_depends_on_list_is_legacy_ready(board):
    g = board.create(user_id="u1", title="solo", depends_on=[])
    assert g.status == STATUS_READY
    assert board.dependencies(g.id) == []


def test_create_with_open_dep_is_waiting(board):
    dep = board.create(user_id="u1", title="prereq")
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    assert g.status == STATUS_WAITING
    assert board.dependencies(g.id) == [dep.id]
    kinds = [e["kind"] for e in board.events(g.id)]
    assert kinds == ["created", "waiting_on_deps"]
    waiting_event = [e for e in board.events(g.id) if e["kind"] == "waiting_on_deps"][0]
    assert waiting_event["payload"]["deps"] == [dep.id]


def test_create_with_already_done_dep_is_ready_legacy(board):
    dep = board.create(user_id="u1", title="prereq")
    _finish(board, dep)
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    assert g.status == STATUS_READY
    # legacy byte-path: no edge, no extra event, when every dep is already done
    assert board.dependencies(g.id) == []
    kinds = [e["kind"] for e in board.events(g.id)]
    assert kinds == ["created"]


def test_create_mixed_deps_writes_full_edge_set(board):
    done_dep = board.create(user_id="u1", title="already-done")
    _finish(board, done_dep)
    open_dep = board.create(user_id="u1", title="still-open")
    g = board.create(user_id="u1", title="dependent",
                      depends_on=[done_dep.id, open_dep.id])
    assert g.status == STATUS_WAITING
    assert set(board.dependencies(g.id)) == {done_dep.id, open_dep.id}
    waiting_event = [e for e in board.events(g.id) if e["kind"] == "waiting_on_deps"][0]
    assert waiting_event["payload"]["deps"] == [open_dep.id]


def test_create_cross_tenant_dep_raises_and_writes_nothing(board):
    other = board.create(user_id="tenant-b", title="not yours")
    with pytest.raises(ValueError):
        board.create(user_id="u1", title="dependent", depends_on=[other.id])
    # no orphan goal row leaked for the rejected create
    assert board.list(user_id="u1") == []


def test_create_unknown_dep_raises_and_writes_nothing(board):
    with pytest.raises(ValueError):
        board.create(user_id="u1", title="dependent", depends_on=["nope-not-real"])
    assert board.list(user_id="u1") == []


def test_create_one_bad_dep_among_good_raises_no_goal_no_edges(board):
    good1 = board.create(user_id="u1", title="good1")
    good2 = board.create(user_id="u1", title="good2")
    with pytest.raises(ValueError):
        board.create(user_id="u1", title="dependent",
                      depends_on=[good1.id, "ghost", good2.id])
    # no new goal row leaked for the rejected create
    titles = {g.title for g in board.list(user_id="u1", limit=1000)}
    assert titles == {"good1", "good2"}
    # all-or-nothing: not even the two valid deps get an edge written
    import sqlite3
    conn = sqlite3.connect(board.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM goal_edges").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_create_dep_on_objective_raises_no_goal_no_edges(board):
    obj = board.create_objective(user_id="u1", title="standing objective")
    with pytest.raises(ValueError):
        board.create(user_id="u1", title="dependent", depends_on=[obj.id])
    titles = {g.title for g in board.list(user_id="u1", limit=1000)}
    assert titles == {"standing objective"}


def test_ready_never_returns_waiting_row(board):
    dep = board.create(user_id="u1", title="prereq")
    waiting_goal = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    ready_ids = {g.id for g in board.ready(limit=50)}
    assert dep.id in ready_ids
    assert waiting_goal.id not in ready_ids
    assert board.get(waiting_goal.id).status == STATUS_WAITING


# --- add_dependencies --------------------------------------------------------

def test_add_dependencies_self_dep_raises(board):
    g = board.create(user_id="u1", title="solo")
    with pytest.raises(ValueError):
        board.add_dependencies(g.id, [g.id], user_id="u1")
    assert board.dependencies(g.id) == []


def test_add_dependencies_direct_cycle_raises(board):
    a = board.create(user_id="u1", title="a")
    b = board.create(user_id="u1", title="b")
    board.add_dependencies(a.id, [b.id], user_id="u1")  # a depends_on b: fine
    with pytest.raises(ValueError):
        # b depends_on a would close a->b->a
        board.add_dependencies(b.id, [a.id], user_id="u1")
    assert board.dependencies(b.id) == []  # rejected edge never written
    assert board.dependencies(a.id) == [b.id]  # original edge untouched


def test_add_dependencies_transitive_cycle_raises(board):
    a = board.create(user_id="u1", title="a")
    b = board.create(user_id="u1", title="b")
    c = board.create(user_id="u1", title="c")
    board.add_dependencies(a.id, [b.id], user_id="u1")  # a depends_on b
    board.add_dependencies(b.id, [c.id], user_id="u1")  # b depends_on c
    with pytest.raises(ValueError):
        # c depends_on a would close a->b->c->a
        board.add_dependencies(c.id, [a.id], user_id="u1")
    assert board.dependencies(c.id) == []


def test_add_dependencies_cross_tenant_raises_no_edge(board):
    a = board.create(user_id="u1", title="a")
    other = board.create(user_id="tenant-b", title="other")
    with pytest.raises(ValueError):
        board.add_dependencies(a.id, [other.id], user_id="u1")
    assert board.dependencies(a.id) == []


def test_add_dependencies_unknown_id_raises_no_edge(board):
    a = board.create(user_id="u1", title="a")
    with pytest.raises(ValueError):
        board.add_dependencies(a.id, ["ghost"], user_id="u1")
    assert board.dependencies(a.id) == []


def test_add_dependencies_rejects_objective_kind(board):
    # OBJ_DONE == STATUS_DONE == "done" (same literal) — without a kind guard
    # an objective could silently "satisfy" a dependency it was never meant
    # to. Edges are goal->goal by design.
    a = board.create(user_id="u1", title="a")
    obj = board.create_objective(user_id="u1", title="standing objective")
    with pytest.raises(ValueError):
        board.add_dependencies(a.id, [obj.id], user_id="u1")
    assert board.dependencies(a.id) == []


def test_add_dependencies_rejects_ask_kind(board):
    a = board.create(user_id="u1", title="a")
    ask = board.create_ask(user_id="u1", what="need owner input")
    with pytest.raises(ValueError):
        board.add_dependencies(a.id, [ask.id], user_id="u1")
    assert board.dependencies(a.id) == []


def test_add_dependencies_all_or_nothing_on_mixed_bad_list(board):
    a = board.create(user_id="u1", title="a")
    good = board.create(user_id="u1", title="good-dep")
    with pytest.raises(ValueError):
        board.add_dependencies(a.id, [good.id, "ghost"], user_id="u1")
    # the valid dep in the same call must NOT have been written either
    assert board.dependencies(a.id) == []


def test_add_dependencies_writes_edge_for_already_done_dep(board):
    dep = board.create(user_id="u1", title="prereq")
    _finish(board, dep)
    a = board.create(user_id="u1", title="a")
    board.add_dependencies(a.id, [dep.id], user_id="u1")
    assert board.dependencies(a.id) == [dep.id]


def test_add_dependencies_is_idempotent(board):
    a = board.create(user_id="u1", title="a")
    b = board.create(user_id="u1", title="b")
    board.add_dependencies(a.id, [b.id], user_id="u1")
    board.add_dependencies(a.id, [b.id], user_id="u1")  # re-add, no error, no dup
    assert board.dependencies(a.id) == [b.id]


def test_add_dependencies_empty_list_is_noop(board):
    a = board.create(user_id="u1", title="a")
    board.add_dependencies(a.id, [], user_id="u1")
    assert board.dependencies(a.id) == []
    kinds = [e["kind"] for e in board.events(a.id)]
    assert "deps_added" not in kinds  # no-op writes nothing, incl. no event


def test_add_dependencies_emits_deps_added_event(board):
    # Edge-only writes must not be invisible to the wake-gate fingerprint
    # (MAX(goal_events.id)) — add_dependencies must emit an event too.
    a = board.create(user_id="u1", title="a")
    b = board.create(user_id="u1", title="b")
    board.add_dependencies(a.id, [b.id], user_id="u1")
    events = board.events(a.id)
    kinds = [e["kind"] for e in events]
    assert "deps_added" in kinds
    event = [e for e in events if e["kind"] == "deps_added"][0]
    assert event["payload"]["deps"] == [b.id]


# --- deps_satisfied ----------------------------------------------------------

def test_deps_satisfied_true_when_no_edges(board):
    g = board.create(user_id="u1", title="solo")
    assert board.deps_satisfied(g.id) is True


def test_deps_satisfied_false_while_dep_open(board):
    dep = board.create(user_id="u1", title="prereq")
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    assert board.deps_satisfied(g.id) is False


def test_deps_satisfied_true_after_dep_done_fresh_read(board):
    dep = board.create(user_id="u1", title="prereq")
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    assert board.deps_satisfied(g.id) is False
    _finish(board, dep)
    # same GoalBoard instance, same goal_id — a fresh read, never cached
    assert board.deps_satisfied(g.id) is True


def test_deps_satisfied_multi_dep_truth_table(board):
    d1 = board.create(user_id="u1", title="d1")
    d2 = board.create(user_id="u1", title="d2")
    g = board.create(user_id="u1", title="dependent", depends_on=[d1.id, d2.id])
    assert board.deps_satisfied(g.id) is False
    _finish(board, d1)
    assert board.deps_satisfied(g.id) is False  # d2 still open
    _finish(board, d2)
    assert board.deps_satisfied(g.id) is True


def test_deps_satisfied_unaffected_by_cancelled_prerequisite(board):
    # Task 1 scope: cancelling a prerequisite does NOT auto-satisfy — a
    # cancelled dep is simply never 'done', so the dependent stays unsatisfied
    # (the dep_failed cascade to 'blocked' is Task 2's job).
    dep = board.create(user_id="u1", title="prereq")
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    board.cancel(dep.id, user_id="u1")
    assert board.deps_satisfied(g.id) is False


# --- Task 2: completion sweep -----------------------------------------------

def test_completion_sweep_partial_deps_stays_waiting(board):
    a = board.create(user_id="u1", title="a")
    b = board.create(user_id="u1", title="b")
    c = board.create(user_id="u1", title="c", depends_on=[a.id, b.id])
    assert c.status == STATUS_WAITING

    _finish(board, a)
    assert board.get(c.id).status == STATUS_WAITING
    kinds = [e["kind"] for e in board.events(c.id)]
    assert "deps_satisfied" not in kinds


def test_completion_sweep_flips_ready_on_last_dep(board):
    a = board.create(user_id="u1", title="a")
    b = board.create(user_id="u1", title="b")
    c = board.create(user_id="u1", title="c", depends_on=[a.id, b.id])

    _finish(board, a)
    _finish(board, b)

    got = board.get(c.id)
    assert got.status == STATUS_READY
    events = [e for e in board.events(c.id) if e["kind"] == "deps_satisfied"]
    assert len(events) == 1
    assert events[0]["payload"]["completed"] == b.id


def test_completion_sweep_concurrent_completions_flip_exactly_once(tmp_path, monkeypatch):
    # Two GoalBoard instances on the SAME db path (simulating two dispatcher
    # workers racing to complete sibling prerequisites), mirroring the
    # `test_claim_is_atomic_single_winner` two-instance pattern.
    #
    # Real contention requires BOTH attempts to observe deps_satisfied()==True
    # for the SAME still-'waiting' dependent before either write lands — if the
    # first worker's automatic record_success sweep is left to run normally, it
    # exits early on deps_satisfied()==False (the sibling isn't done yet) and
    # never reaches the UPDATE at all, so there is nothing for the CAS guard to
    # arbitrate. To construct the actual race, the automatic sweep is
    # suppressed while both prerequisites complete (so C is still 'waiting'
    # with BOTH deps done), then the private sweep is invoked directly on both
    # instances back-to-back — exactly the "two workers both saw satisfied,
    # both tried to flip" scenario the `AND status='waiting'` CAS exists for.
    db = str(tmp_path / "goals.db")
    b1 = GoalBoard(db)
    b2 = GoalBoard(db)

    a = b1.create(user_id="u1", title="a")
    b = b1.create(user_id="u1", title="b")
    c = b1.create(user_id="u1", title="c", depends_on=[a.id, b.id])
    assert c.status == STATUS_WAITING

    with monkeypatch.context() as m:
        m.setattr(GoalBoard, "_sweep_dependents_on_completion", lambda self, goal_id: None)
        b1.claim(a.id, "w1", ttl_seconds=900)
        b1.record_success(a.id, session_id="s1", result="ok")
        b2.claim(b.id, "w2", ttl_seconds=900)
        b2.record_success(b.id, session_id="s2", result="ok")

    # Both prerequisites are 'done' but C never got swept — still 'waiting',
    # and BOTH board instances now see it as satisfied.
    assert b1.get(c.id).status == STATUS_WAITING
    assert b1.deps_satisfied(c.id) is True
    assert b2.deps_satisfied(c.id) is True

    # The race: two workers, both having just observed deps_satisfied()==True
    # for the same 'waiting' row, both attempt the flip back-to-back.
    b1._sweep_dependents_on_completion(a.id)
    b2._sweep_dependents_on_completion(b.id)

    got = b1.get(c.id)
    assert got.status == STATUS_READY
    # Exactly one flip — the loser's CAS (rc=0) must not double-fire the event.
    # (Proven: stripping the `AND status='waiting'` guard from the UPDATE in
    # _sweep_dependents_on_completion makes this assert fail with 2 events.)
    events = [e for e in b1.events(c.id) if e["kind"] == "deps_satisfied"]
    assert len(events) == 1


def test_completion_sweep_noop_when_no_dependents(board):
    a = board.create(user_id="u1", title="lonely")
    _finish(board, a)  # must not raise
    assert board.get(a.id).status == "done"


def test_completion_sweep_never_resurrects_a_cancelled_dependent(board):
    a = board.create(user_id="u1", title="a")
    b = board.create(user_id="u1", title="b")
    c = board.create(user_id="u1", title="c", depends_on=[a.id, b.id])
    board.cancel(c.id, user_id="u1")  # owner cancels the dependent itself
    _finish(board, a)
    _finish(board, b)
    assert board.get(c.id).status == STATUS_CANCELLED


# --- Task 2: dep_failed inverse cascade -------------------------------------

def test_cancelled_prerequisite_cascades_dependent_to_blocked_dep_failed(board):
    dep = board.create(user_id="u1", title="prereq")
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    assert g.status == STATUS_WAITING

    board.cancel(dep.id, user_id="u1")

    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload["block_kind"] == "dep_failed"
    events = [e for e in board.events(g.id) if e["kind"] == "dep_failed"]
    assert len(events) == 1
    assert events[0]["payload"]["prerequisite"] == dep.id


def test_record_failure_breaker_trip_cascades_dependent_to_blocked_dep_failed(board):
    dep = board.create(user_id="u1", title="prereq", max_retries=1)
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])

    board.claim(dep.id, "w", ttl_seconds=900)
    after = board.record_failure(dep.id, error="boom")
    assert after.status == STATUS_BLOCKED  # max_retries=1 -> breaker tripped

    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload["block_kind"] == "dep_failed"
    events = [e for e in board.events(g.id) if e["kind"] == "dep_failed"]
    assert len(events) == 1
    assert events[0]["payload"]["prerequisite"] == dep.id


def test_record_failure_below_threshold_does_not_cascade(board):
    dep = board.create(user_id="u1", title="prereq", max_retries=3)
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])

    board.claim(dep.id, "w", ttl_seconds=900)
    after = board.record_failure(dep.id, error="transient")
    assert after.status == STATUS_READY  # breaker not tripped yet

    got = board.get(g.id)
    assert got.status == STATUS_WAITING  # untouched
    assert "dep_failed" not in [e["kind"] for e in board.events(g.id)]


def test_reclaim_stale_breaker_trip_cascades_dependent_to_blocked_dep_failed(tmp_path):
    clock = _Clock()
    b = GoalBoard(str(tmp_path / "goals.db"), clock=clock)
    dep = b.create(user_id="u1", title="poison", max_retries=1)
    g = b.create(user_id="u1", title="dependent", depends_on=[dep.id])

    b.claim(dep.id, "w1", ttl_seconds=100)
    clock.advance(200)  # claim TTL expired — worker crashed
    n = b.reclaim_stale()
    assert n == 1
    assert b.get(dep.id).status == STATUS_BLOCKED

    got = b.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload["block_kind"] == "dep_failed"
    events = [e for e in b.events(g.id) if e["kind"] == "dep_failed"]
    assert len(events) == 1
    assert events[0]["payload"]["prerequisite"] == dep.id


def test_reclaim_stale_requeue_below_threshold_does_not_cascade(tmp_path):
    clock = _Clock()
    b = GoalBoard(str(tmp_path / "goals.db"), clock=clock)
    dep = b.create(user_id="u1", title="transient", max_retries=3)
    g = b.create(user_id="u1", title="dependent", depends_on=[dep.id])

    b.claim(dep.id, "w1", ttl_seconds=100)
    clock.advance(200)
    b.reclaim_stale()
    assert b.get(dep.id).status == STATUS_READY

    got = b.get(g.id)
    assert got.status == STATUS_WAITING
    assert "dep_failed" not in [e["kind"] for e in b.events(g.id)]


def test_dep_failed_cascade_only_touches_waiting_dependents(board):
    # A dependent that already progressed off 'waiting' (e.g. was cancelled by
    # the owner) must not be resurrected/overwritten by a late cascade.
    dep = board.create(user_id="u1", title="prereq")
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    board.cancel(g.id, user_id="u1")
    board.cancel(dep.id, user_id="u1")
    assert board.get(g.id).status == STATUS_CANCELLED


# --- Task 2: owner unblock override on a dep_failed row ---------------------

def test_owner_unblock_on_dep_failed_dependent_reenters_ready(board):
    dep = board.create(user_id="u1", title="prereq")
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    board.cancel(dep.id, user_id="u1")
    assert board.get(g.id).status == STATUS_BLOCKED

    ok = board.unblock(g.id, user_id="u1", rationale="owner override")
    assert ok is True
    got = board.get(g.id)
    # Re-enters ready regardless of the still-cancelled (never satisfiable)
    # prerequisite — the owner's decision wins, deps_satisfied is not re-checked.
    assert got.status == STATUS_READY
    assert board.deps_satisfied(g.id) is False


# --- Task 2: creation-time terminal-bad-dep closure -------------------------

def test_create_with_already_cancelled_dep_is_immediately_blocked_dep_failed(board):
    dep = board.create(user_id="u1", title="prereq")
    board.cancel(dep.id, user_id="u1")

    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])

    assert g.status == STATUS_BLOCKED
    assert g.payload["block_kind"] == "dep_failed"
    # never rejected — the goal is preserved for owner unblock
    assert board.get(g.id) is not None
    assert board.dependencies(g.id) == [dep.id]
    events = [e["kind"] for e in board.events(g.id)]
    assert events == ["created", "dep_failed"]
    dep_failed_event = [e for e in board.events(g.id) if e["kind"] == "dep_failed"][0]
    assert dep_failed_event["payload"]["prerequisites"] == [dep.id]


def test_create_with_breaker_blocked_dep_is_immediately_blocked_dep_failed(board):
    dep = board.create(user_id="u1", title="prereq", max_retries=1)
    board.claim(dep.id, "w", ttl_seconds=900)
    board.record_failure(dep.id, error="boom")  # trips breaker -> blocked, exhausted
    assert board.get(dep.id).status == STATUS_BLOCKED

    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])

    assert g.status == STATUS_BLOCKED
    assert g.payload["block_kind"] == "dep_failed"


def test_create_with_agent_declared_block_not_exhausted_is_waiting_not_blocked(board):
    # block_from_ready (agent-declared BLOCKED) may fire well before
    # consecutive_failures reaches max_retries — that is NOT "exhausted
    # retries", so a new dependent still waits (today's latent-forever-wait
    # behavior for this specific sub-case is unchanged; only the
    # breaker-exhausted / cancelled cases get the immediate closure).
    dep = board.create(user_id="u1", title="prereq", max_retries=5)
    board.block_from_ready(dep.id, error="agent says unrunnable")
    assert board.get(dep.id).status == STATUS_BLOCKED
    assert board.get(dep.id).consecutive_failures == 0

    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    assert g.status == STATUS_WAITING


def test_create_mixed_terminal_bad_and_open_dep_is_blocked_not_waiting(board):
    cancelled_dep = board.create(user_id="u1", title="dead")
    board.cancel(cancelled_dep.id, user_id="u1")
    open_dep = board.create(user_id="u1", title="still-open")

    g = board.create(user_id="u1", title="dependent",
                      depends_on=[cancelled_dep.id, open_dep.id])

    assert g.status == STATUS_BLOCKED
    assert g.payload["block_kind"] == "dep_failed"
    assert set(board.dependencies(g.id)) == {cancelled_dep.id, open_dep.id}


# --- T2.1 final-review Fix 1: unblock / unblocked_by_ask clear block_kind ---

def test_unblock_clears_block_kind_from_payload(board):
    dep = board.create(user_id="u1", title="prereq")
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    board.cancel(dep.id, user_id="u1")  # cascades g -> blocked/dep_failed
    assert board.get(g.id).payload.get("block_kind") == "dep_failed"

    ok = board.unblock(g.id, user_id="u1", rationale="owner override")
    assert ok is True
    got = board.get(g.id)
    assert got.status == STATUS_READY
    assert "block_kind" not in got.payload


def test_ask_fulfillment_unblock_clears_block_kind(board):
    dep = board.create(user_id="u1", title="prereq")
    g = board.create(user_id="u1", title="dependent", depends_on=[dep.id])
    board.cancel(dep.id, user_id="u1")  # cascades g -> blocked/dep_failed
    assert board.get(g.id).payload.get("block_kind") == "dep_failed"

    ask = board.create_ask(user_id="u1", what="unblock please",
                            blocks_goal_ids=[g.id])
    ok, unblocked = board.decide_ask(ask.id, user_id="u1", approved=True)
    assert ok is True
    assert unblocked == 1
    got = board.get(g.id)
    assert got.status == STATUS_READY
    assert "block_kind" not in got.payload
    # the fulfillment stamp itself must still land alongside the clear
    assert got.payload.get("owner_unblocked", {}).get("ask_id") == ask.id


# --- T2.1 final-review Fix 2: dep_failed dependents DAG-revive -------------

def test_dep_failed_dependent_revives_when_prerequisite_completes_after_unblock(board):
    p = board.create(user_id="u1", title="p", max_retries=1)
    d = board.create(user_id="u1", title="d", depends_on=[p.id])
    assert d.status == STATUS_WAITING

    board.claim(p.id, "w", ttl_seconds=900)
    board.record_failure(p.id, error="boom")  # trips breaker -> blocked
    assert board.get(p.id).status == STATUS_BLOCKED
    dep_got = board.get(d.id)
    assert dep_got.status == STATUS_BLOCKED
    assert dep_got.payload.get("block_kind") == "dep_failed"

    assert board.unblock(p.id, user_id="u1") is True
    assert board.get(p.id).status == STATUS_READY

    board.claim(p.id, "w", ttl_seconds=900)
    board.record_success(p.id, session_id="s", result="ok")

    revived = board.get(d.id)
    assert revived.status == STATUS_READY
    assert "block_kind" not in revived.payload
    events = [e for e in board.events(d.id) if e["kind"] == "deps_satisfied"]
    assert len(events) == 1
    assert events[0]["payload"]["completed"] == p.id


def test_dep_failed_dependent_stays_blocked_while_a_sibling_dep_still_open(board):
    p1 = board.create(user_id="u1", title="p1", max_retries=1)
    p2 = board.create(user_id="u1", title="p2")
    d = board.create(user_id="u1", title="d", depends_on=[p1.id, p2.id])
    assert d.status == STATUS_WAITING

    board.claim(p1.id, "w", ttl_seconds=900)
    board.record_failure(p1.id, error="boom")
    assert board.get(d.id).payload.get("block_kind") == "dep_failed"

    board.unblock(p1.id, user_id="u1")
    board.claim(p1.id, "w", ttl_seconds=900)
    board.record_success(p1.id, session_id="s", result="ok")

    # p2 is still open -> d must NOT revive yet
    got = board.get(d.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "dep_failed"


# --- T2.1 final-review Fix 3: reconcile_waiting -----------------------------

def test_reconcile_waiting_flips_stranded_satisfied_row_to_ready(board, monkeypatch):
    a = board.create(user_id="u1", title="a")
    c = board.create(user_id="u1", title="c", depends_on=[a.id])
    assert c.status == STATUS_WAITING

    with monkeypatch.context() as m:
        # Manufacture the strand window: the prerequisite completes but the
        # dependents sweep never fires (crash-between-CAS-and-sweep, or the
        # create-writes-row-then-edges race).
        m.setattr(GoalBoard, "_sweep_dependents_on_completion", lambda self, goal_id: None)
        board.claim(a.id, "w", ttl_seconds=900)
        board.record_success(a.id, session_id="s", result="ok")

    assert board.get(a.id).status == STATUS_DONE
    assert board.get(c.id).status == STATUS_WAITING  # stranded
    assert board.deps_satisfied(c.id) is True

    n = board.reconcile_waiting()
    assert n == 1
    got = board.get(c.id)
    assert got.status == STATUS_READY
    events = [e for e in board.events(c.id) if e["kind"] == "deps_satisfied"]
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == "reconciled"


def test_reconcile_waiting_cascades_dep_failed_for_stranded_cancelled_prerequisite(
        board, monkeypatch):
    p = board.create(user_id="u1", title="p")
    d = board.create(user_id="u1", title="d", depends_on=[p.id])
    assert d.status == STATUS_WAITING

    with monkeypatch.context() as m:
        # Manufacture a missed cascade (the strand window).
        m.setattr(GoalBoard, "_cascade_dep_failed", lambda self, goal_id: None)
        board.cancel(p.id, user_id="u1")

    assert board.get(p.id).status == STATUS_CANCELLED
    assert board.get(d.id).status == STATUS_WAITING  # stranded, cascade missed

    n = board.reconcile_waiting()
    assert n == 1
    got = board.get(d.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "dep_failed"
    events = [e for e in board.events(d.id) if e["kind"] == "dep_failed"]
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == "reconciled"


def test_reconcile_waiting_cascades_dep_failed_for_stranded_breaker_exhausted_prerequisite(
        board, monkeypatch):
    p = board.create(user_id="u1", title="p", max_retries=1)
    d = board.create(user_id="u1", title="d", depends_on=[p.id])
    assert d.status == STATUS_WAITING

    with monkeypatch.context() as m:
        m.setattr(GoalBoard, "_cascade_dep_failed", lambda self, goal_id: None)
        board.claim(p.id, "w", ttl_seconds=900)
        board.record_failure(p.id, error="boom")  # trips breaker -> blocked

    assert board.get(p.id).status == STATUS_BLOCKED
    assert board.get(p.id).consecutive_failures >= board.get(p.id).max_retries
    assert board.get(d.id).status == STATUS_WAITING  # stranded, cascade missed

    n = board.reconcile_waiting()
    assert n == 1
    got = board.get(d.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "dep_failed"


def test_reconcile_waiting_leaves_live_waiting_row_untouched(board):
    p = board.create(user_id="u1", title="p")  # still ready — genuinely live
    d = board.create(user_id="u1", title="d", depends_on=[p.id])
    assert d.status == STATUS_WAITING

    n = board.reconcile_waiting()
    assert n == 0
    assert board.get(d.id).status == STATUS_WAITING
    assert "deps_satisfied" not in [e["kind"] for e in board.events(d.id)]
    assert "dep_failed" not in [e["kind"] for e in board.events(d.id)]


def test_reconcile_waiting_ignores_non_waiting_rows(board):
    a = board.create(user_id="u1", title="solo ready")
    assert board.reconcile_waiting() == 0
    assert board.get(a.id).status == STATUS_READY


def test_add_dependencies_raced_cycle_repaired_post_insert(board, monkeypatch):
    """2026-07-23 validation (P3, latent): the pre-insert cycle check is
    check-then-write — two concurrent add_dependencies can both pass it and a
    formed cycle strands both waiting rows forever. Simulate the race window
    (pre-check blind, post-check live) and prove the post-insert re-verify
    rolls back the caller's own edges and raises."""
    a = board.create(user_id="u1", title="a")
    b = board.create(user_id="u1", title="b")
    board.add_dependencies(a.id, [b.id], user_id="u1")

    real = board._would_close_cycle
    calls = {"n": 0}

    def racy(goal_id, dep_id, **kw):
        calls["n"] += 1
        if calls["n"] == 1:  # the pre-insert check: simulate the lost race
            return False
        return real(goal_id, dep_id, **kw)

    monkeypatch.setattr(board, "_would_close_cycle", racy)
    with pytest.raises(ValueError, match="post-insert"):
        board.add_dependencies(b.id, [a.id], user_id="u1")
    assert board.dependencies(b.id) == []          # raced-in edge rolled back
    assert board.dependencies(a.id) == [b.id]      # pre-existing edge untouched
