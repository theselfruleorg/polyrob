"""Typed block kinds + kind-aware blocked aging (T2.1 Task 3, review-fixed 2026-07-23).

``payload.block_kind`` is a payload discriminator (no new column) stamped
through a SINGLE choke point — ``dispatcher._maybe_escalate_blocked``'s
``block_kind_hint`` param:

- ``dep_failed`` — create-time / completion-cascade closure (Task 2).
- ``provider_outage`` — classified once (``_is_llm_provider_exhausted``) at
  EVERY failure call site (``_fail_run``, the refusal-status path, and the
  top-level ``except Exception`` handler — the latter two never call
  ``_fail_run`` at all, which was review finding #1: they used to bypass the
  stamp entirely and land the generic ``needs_input`` kind instead) and
  passed into ``_maybe_escalate_blocked`` as a hint, stamped ``only_if_absent``.
- ``needs_input`` — ``_maybe_escalate_blocked``'s generic fallback, stamped
  ONLY when no more specific kind is already set.

``GoalBoard.age_out_blocked`` reads the kind to decide whether/how a blocked
goal can self-heal: ``provider_outage`` requeues on a short window
(``GOAL_BLOCKED_PROVIDER_RETRY_MIN``), capped at
``_PROVIDER_OUTAGE_MAX_REQUEUES`` requeues (review finding #2 — an ordinary,
non-sentinel provider death must not retry forever; each requeue resets
``consecutive_failures`` so the breaker alone never bounds it). Past the cap,
and for ``needs_input``/``dep_failed`` always, the row is NEVER auto-requeued
— but (review finding #3, adjudicated) it IS still subject to the legacy
terminal max-age-out-to-cancelled rail, same as an absent/unknown kind:
exempting a kind from requeue must not ALSO exempt it from ever aging out.
"""
import asyncio

import pytest

from agents.task.goals.board import (
    GoalBoard, STATUS_BLOCKED, STATUS_CANCELLED, STATUS_READY, STATUS_WAITING,
)
from agents.task.goals.dispatcher import GoalDispatcher


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _FakeAgent:
    """Never actually run — only the board-level failure helpers are exercised
    (GOAL_BLOCKER_ESCALATION defaults off in tests, so `.container` is unused)."""


class _RefusalAgent:
    """Drives ``_run_goal``'s REFUSAL-status branch end-to-end (review finding
    #1) — ``run_session`` returns a known refusal prefix (``is_refusal``)
    carrying the provider-exhaustion phrasing, never raising."""

    def __init__(self, status):
        self.status = status

    async def create_session(self, *, user_id, request):
        return {"id": f"sess-{user_id}"}

    async def run_session(self, user_id, session_id):
        return self.status

    def get_orchestrator(self, session_id):
        return None


class _ExceptionAgent:
    """Drives ``_run_goal``'s top-level ``except Exception`` branch end-to-end
    (review finding #1) — ``run_session`` raises directly."""

    def __init__(self, message):
        self.message = message

    async def create_session(self, *, user_id, request):
        return {"id": f"sess-{user_id}"}

    async def run_session(self, user_id, session_id):
        raise RuntimeError(self.message)

    def get_orchestrator(self, session_id):
        return None


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


@pytest.fixture
def dispatcher(board):
    return GoalDispatcher(board, _FakeAgent())


@pytest.fixture
def clocked_board(tmp_path):
    clock = _Clock()
    return GoalBoard(str(tmp_path / "goals.db"), clock=clock), clock


def _block_with_kind(board, *, max_retries=1, block_kind=None):
    """Trip the breaker on a fresh goal (-> blocked, unstamped), then
    optionally stamp a block_kind directly (mirrors what the real producers
    would have written)."""
    g = board.create(user_id="u1", title="t", max_retries=max_retries)
    board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="boom")
    assert board.get(g.id).status == STATUS_BLOCKED
    if block_kind is not None:
        board.stamp_block_kind(g.id, block_kind)
    return g


# ---------------------------------------------------------------------------
# stamp: provider_outage (_fail_run)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fail_run_stamps_provider_outage_when_exhausted(board, dispatcher):
    g = board.create(user_id="u1", title="flaky", max_retries=1)
    board.claim(g.id, "w1", ttl_seconds=900)
    await dispatcher._fail_run(
        g, None, error="llm_provider_exhausted: ALL LLM PROVIDERS EXHAUSTED")
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "provider_outage"
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "block_kind_set" in kinds


@pytest.mark.asyncio
async def test_fail_run_does_not_stamp_provider_outage_for_ordinary_failure(board, dispatcher):
    g = board.create(user_id="u1", title="flaky", max_retries=1)
    board.claim(g.id, "w1", ttl_seconds=900)
    await dispatcher._fail_run(g, None, error="acceptance checks failed: no output produced")
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    # not provider-exhausted-classified -> _fail_run stamps nothing; the
    # _maybe_escalate_blocked call it makes internally fills the generic kind.
    assert got.payload.get("block_kind") == "needs_input"


@pytest.mark.asyncio
async def test_fail_run_does_not_stamp_when_row_stays_ready(board, dispatcher):
    g = board.create(user_id="u1", title="retry me", max_retries=3)
    board.claim(g.id, "w1", ttl_seconds=900)
    await dispatcher._fail_run(
        g, None, error="llm_provider_exhausted: ALL LLM PROVIDERS EXHAUSTED")
    got = board.get(g.id)
    assert got.status == STATUS_READY  # breaker not tripped yet (max_retries=3)
    assert got.payload.get("block_kind") is None


@pytest.mark.asyncio
async def test_fail_run_block_declared_path_also_stamps_provider_outage(board, dispatcher):
    """``block=True`` (agent-declared BLOCKED via block_from_ready) is the
    OTHER route _fail_run drives a row to 'blocked' through — must classify
    the failure the same way as the breaker-trip route."""
    g = board.create(user_id="u1", title="declared blocked", max_retries=5)
    board.claim(g.id, "w1", ttl_seconds=900)
    await dispatcher._fail_run(
        g, None,
        error="agent declared BLOCKED: llm_provider_exhausted: All LLM providers failed",
        block=True)
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "provider_outage"


# ---------------------------------------------------------------------------
# CRITICAL review finding #1: the refusal-status path and the top-level
# except-Exception handler in _run_goal never call _fail_run at all — they
# call record_failure/_maybe_escalate_blocked DIRECTLY. Exercised end-to-end
# through dispatch_once() so a regression that reintroduces the bypass fails
# these, not just a unit-level _maybe_escalate_blocked call.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refusal_path_provider_death_stamps_provider_outage(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    g = board.create(user_id="u1", title="flaky", max_retries=1)
    agent = _RefusalAgent("Session failed: PERMANENT ERROR: ALL LLM PROVIDERS EXHAUSTED")
    d = GoalDispatcher(board, agent)
    n = await d.dispatch_once()
    assert n == 1
    await asyncio.sleep(0.05)
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "provider_outage"


@pytest.mark.asyncio
async def test_refusal_path_ordinary_refusal_stamps_needs_input(board, monkeypatch):
    """Sanity check on the OTHER branch: an ordinary (non-provider) refusal
    still falls to the generic needs_input kind — the hint mechanism must not
    over-fire on every refusal."""
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    g = board.create(user_id="u1", title="flaky", max_retries=1)
    agent = _RefusalAgent("session not found or unauthorized")
    d = GoalDispatcher(board, agent)
    await d.dispatch_once()
    await asyncio.sleep(0.05)
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "needs_input"


@pytest.mark.asyncio
async def test_exception_path_provider_death_stamps_provider_outage(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    g = board.create(user_id="u1", title="flaky", max_retries=1)
    agent = _ExceptionAgent("ALL LLM PROVIDERS EXHAUSTED (raw exception)")
    d = GoalDispatcher(board, agent)
    n = await d.dispatch_once()
    assert n == 1
    await asyncio.sleep(0.05)
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "provider_outage"


@pytest.mark.asyncio
async def test_exception_path_ordinary_exception_stamps_needs_input(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    g = board.create(user_id="u1", title="flaky", max_retries=1)
    agent = _ExceptionAgent("boom, unrelated to providers")
    d = GoalDispatcher(board, agent)
    await d.dispatch_once()
    await asyncio.sleep(0.05)
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "needs_input"


@pytest.mark.asyncio
async def test_maybe_escalate_blocked_hint_stamped_before_needs_input_fallback(board, dispatcher):
    """Unit-level proof of the choke-point mechanism itself: block_kind_hint
    lands, and the needs_input fallback that runs right after it is a refused
    no-op (only_if_absent), not a second event."""
    g = board.create(user_id="u1", title="t", max_retries=1)
    board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="boom")  # trips breaker, unstamped
    got = board.get(g.id)
    assert got.payload.get("block_kind") is None

    await dispatcher._maybe_escalate_blocked(got, block_kind_hint="provider_outage")
    got2 = board.get(g.id)
    assert got2.payload.get("block_kind") == "provider_outage"
    stamps = [e for e in board.events(g.id) if e["kind"] == "block_kind_set"]
    assert len(stamps) == 1
    assert stamps[0]["payload"]["block_kind"] == "provider_outage"


# ---------------------------------------------------------------------------
# stamp: needs_input (_maybe_escalate_blocked) + precedence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_escalate_blocked_stamps_needs_input_when_absent(board, dispatcher):
    g = board.create(user_id="u1", title="t", max_retries=1)
    board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="ordinary failure")  # trips breaker, no kind stamped
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") is None

    await dispatcher._maybe_escalate_blocked(got)
    got2 = board.get(g.id)
    assert got2.payload.get("block_kind") == "needs_input"
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "block_kind_set" in kinds


@pytest.mark.asyncio
async def test_maybe_escalate_blocked_never_overwrites_dep_failed(board, dispatcher):
    prereq = board.create(user_id="u1", title="prereq")
    dependent = board.create(user_id="u1", title="dependent", depends_on=[prereq.id])
    board.cancel(prereq.id, user_id="u1")  # cascades dependent -> blocked/dep_failed
    dep_got = board.get(dependent.id)
    assert dep_got.status == STATUS_BLOCKED
    assert dep_got.payload.get("block_kind") == "dep_failed"

    await dispatcher._maybe_escalate_blocked(dep_got)
    dep_got2 = board.get(dependent.id)
    assert dep_got2.payload.get("block_kind") == "dep_failed"  # unchanged — precedence


@pytest.mark.asyncio
async def test_maybe_escalate_blocked_never_overwrites_provider_outage(board, dispatcher):
    """Reached via the real _fail_run path: provider_outage is stamped BEFORE
    _fail_run's own internal call to _maybe_escalate_blocked, which must not
    then clobber it with needs_input."""
    g = board.create(user_id="u1", title="flaky", max_retries=1)
    board.claim(g.id, "w1", ttl_seconds=900)
    await dispatcher._fail_run(
        g, None, error="llm_provider_exhausted: ALL LLM PROVIDERS EXHAUSTED")
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "provider_outage"
    # exactly ONE block_kind_set event (the provider_outage stamp) — the
    # needs_input attempt inside _maybe_escalate_blocked was a refused no-op.
    stamps = [e for e in board.events(g.id) if e["kind"] == "block_kind_set"]
    assert len(stamps) == 1
    assert stamps[0]["payload"]["block_kind"] == "provider_outage"


def test_stamp_block_kind_refuses_non_blocked_row(board):
    g = board.create(user_id="u1", title="ready goal")
    assert board.stamp_block_kind(g.id, "needs_input") is False


def test_stamp_block_kind_is_a_noop_on_same_value(board):
    g = _block_with_kind(board, block_kind="needs_input")
    n_events_before = len(board.events(g.id))
    assert board.stamp_block_kind(g.id, "needs_input") is False
    assert len(board.events(g.id)) == n_events_before  # no duplicate event


# ---------------------------------------------------------------------------
# kind-aware aging truth table
# ---------------------------------------------------------------------------

def test_aging_requeues_provider_outage_after_short_window(clocked_board):
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="provider_outage")
    clock.advance(31 * 60)  # past the default 30-minute retry window
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 1
    got = board.get(g.id)
    assert got.status == STATUS_READY
    assert got.consecutive_failures == 0
    events = [e["kind"] for e in board.events(g.id)]
    assert "provider_outage_retried" in events
    assert "aged_out" not in events


def test_aging_leaves_fresh_provider_outage_blocked(clocked_board):
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="provider_outage")
    clock.advance(5 * 60)  # inside the window
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 0
    assert board.get(g.id).status == STATUS_BLOCKED


def test_aging_needs_input_not_requeued_while_recent(clocked_board):
    """T2.1 Task-3 review adjudication (finding #3): needs_input is exempt
    from auto-REQUEUE only. Within the legacy max-age window it stays
    untouched -- not requeued, not aged."""
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="needs_input")
    clock.advance(5 * 86400)  # inside max_age_days=14
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 0
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED  # never READY at any point
    assert got.payload.get("block_kind") == "needs_input"


def test_aging_needs_input_terminally_ages_to_cancelled_when_ancient(clocked_board):
    """...but past max_age_days it DOES terminally age out (never silently
    immortal) -- the legacy terminal rail applies to every kind."""
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="needs_input")
    clock.advance(20 * 86400)  # past max_age_days=14
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 1
    got = board.get(g.id)
    assert got.status == STATUS_CANCELLED
    events = [e["kind"] for e in board.events(g.id)]
    assert "aged_out" in events
    assert "provider_outage_retried" not in events  # never short-requeued


def test_aging_dep_failed_not_requeued_while_recent(clocked_board):
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="dep_failed")
    clock.advance(5 * 86400)
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 0
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "dep_failed"


def test_aging_dep_failed_terminally_ages_to_cancelled_with_cascade(clocked_board):
    """dep_failed also terminally ages (finding #3) -- and, being a real DAG
    dependent with its OWN dependent (grandchild), the age-out cascade
    (_cascade_dep_failed) fires again on the transition, exactly like any
    other terminal-bad prerequisite."""
    board, clock = clocked_board
    prereq = board.create(user_id="u1", title="prereq", max_retries=5)
    dependent = board.create(user_id="u1", title="dependent", depends_on=[prereq.id])
    board.cancel(prereq.id, user_id="u1")  # cascades dependent -> blocked/dep_failed
    dep_got = board.get(dependent.id)
    assert dep_got.status == STATUS_BLOCKED
    assert dep_got.payload.get("block_kind") == "dep_failed"

    grandchild = board.create(user_id="u1", title="grandchild", depends_on=[dependent.id])
    assert grandchild.status == STATUS_WAITING  # dependent isn't terminal-bad by breaker count

    clock.advance(20 * 86400)  # past max_age_days=14
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 1
    assert board.get(dependent.id).status == STATUS_CANCELLED

    grandchild_got = board.get(grandchild.id)
    assert grandchild_got.status == STATUS_BLOCKED
    assert grandchild_got.payload.get("block_kind") == "dep_failed"


def test_aging_unstamped_row_ages_to_cancelled_legacy_default(clocked_board):
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind=None)  # legacy: no block_kind at all
    clock.advance(20 * 86400)  # past max_age_days=14
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 1
    got = board.get(g.id)
    assert got.status == STATUS_CANCELLED
    assert "aged_out" in [e["kind"] for e in board.events(g.id)]


def test_aging_unstamped_recent_row_is_untouched_legacy_default(clocked_board):
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind=None)
    clock.advance(5 * 86400)  # inside max_age_days=14
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 0
    assert board.get(g.id).status == STATUS_BLOCKED


def test_aging_unknown_kind_falls_back_to_legacy_default(clocked_board):
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="something_we_never_defined")
    clock.advance(20 * 86400)
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 1
    assert board.get(g.id).status == STATUS_CANCELLED


def test_aging_zero_max_age_days_disables_whole_sweep(clocked_board):
    """max_age_days<=0 is the existing disable sentinel — the whole sweep
    (incl. the provider_outage rail) is a no-op, byte-identical to legacy
    (the dispatcher's own call site already gates the call this way)."""
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="provider_outage")
    clock.advance(400 * 86400)
    n = board.age_out_blocked(max_age_days=0, provider_retry_min=30)
    assert n == 0
    assert board.get(g.id).status == STATUS_BLOCKED


def test_aging_resolves_provider_retry_min_from_env_when_not_passed(clocked_board, monkeypatch):
    board, clock = clocked_board
    monkeypatch.setenv("GOAL_BLOCKED_PROVIDER_RETRY_MIN", "1")  # 1 minute
    g = _block_with_kind(board, block_kind="provider_outage")
    clock.advance(90)  # 1.5 minutes — past the 1-minute env window
    n = board.age_out_blocked(max_age_days=14)  # provider_retry_min NOT passed
    assert n == 1
    assert board.get(g.id).status == STATUS_READY


# ---------------------------------------------------------------------------
# IMPORTANT review finding #2: provider_outage requeue cap — an ordinary
# (non-sentinel) provider death must not retry forever. Each requeue resets
# consecutive_failures, so the breaker alone never bounds the cycle count.
# ---------------------------------------------------------------------------

def test_provider_outage_requeue_cap_stops_after_16_and_falls_to_legacy_rail(clocked_board):
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="provider_outage")
    for i in range(16):
        clock.advance(31 * 60)
        n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
        assert n == 1
        got = board.get(g.id)
        assert got.status == STATUS_READY
        assert got.payload.get("provider_requeues") == i + 1
        # re-trip the breaker so the row is blocked+provider_outage again for
        # the next iteration (block_kind persists across a payload merge-write).
        board.claim(g.id, "w1", ttl_seconds=900)
        board.record_failure(g.id, error="boom")
        got2 = board.get(g.id)
        assert got2.status == STATUS_BLOCKED
        assert got2.payload.get("block_kind") == "provider_outage"

    # 17th tick, past the short window again: cap reached -> NO 17th requeue,
    # exhausted stamp instead (exactly once), row still 'blocked'.
    clock.advance(31 * 60)
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 0  # nothing transitioned this tick
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED  # NOT requeued to ready
    assert got.payload.get("provider_retry_exhausted") is True
    events = [e["kind"] for e in board.events(g.id)]
    assert events.count("provider_retry_exhausted") == 1
    assert events.count("provider_outage_retried") == 16  # no 17th

    # re-running immediately must not re-fire the exhausted event (idempotent).
    n_repeat = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n_repeat == 0
    events2 = [e["kind"] for e in board.events(g.id)]
    assert events2.count("provider_retry_exhausted") == 1  # still exactly once

    # and the row still terminal-ages once it clears the (long) legacy window.
    clock.advance(15 * 86400)
    n2 = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n2 == 1
    got3 = board.get(g.id)
    assert got3.status == STATUS_CANCELLED
    assert "aged_out" in [e["kind"] for e in board.events(g.id)]


# ---------------------------------------------------------------------------
# age-out-to-cancelled cascades dep_failed to dependents (Task-2 review flag)
#
# NOTE ON FIXTURE SHAPE: a prerequisite that trips the circuit breaker via
# record_failure/reclaim_stale ALREADY cascades dep_failed to its WAITING
# dependents at THAT moment (Task 2) — so a dependent of a breaker-tripped
# prereq is dep_failed long before age_out_blocked ever runs, and re-testing
# that path here wouldn't exercise the NEW aging-time cascade at all. The
# genuinely uncovered case (the one the review flag calls out) is
# ``block_from_ready`` (agent-declared BLOCKED, retries NOT exhausted) —
# it does not cascade at block-time (test_create_with_agent_declared_block_
# not_exhausted_is_waiting_not_blocked's documented "latent-forever-wait"
# sub-case) — so a dependent sits WAITING until this prereq is later resolved
# by the owner OR ages out.
# ---------------------------------------------------------------------------

def test_ageout_to_cancelled_cascades_dep_failed_to_dependents(clocked_board):
    board, clock = clocked_board
    prereq = board.create(user_id="u1", title="prereq", max_retries=5)
    dependent = board.create(user_id="u1", title="dependent", depends_on=[prereq.id])
    assert dependent.status == STATUS_WAITING

    board.block_from_ready(prereq.id, error="agent says unrunnable")
    assert board.get(prereq.id).status == STATUS_BLOCKED
    assert board.get(prereq.id).payload.get("block_kind") is None
    assert board.get(dependent.id).status == STATUS_WAITING  # not cascaded yet

    clock.advance(20 * 86400)  # past max_age_days=14 -> ages out to cancelled
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 1
    assert board.get(prereq.id).status == STATUS_CANCELLED

    dep_got = board.get(dependent.id)
    assert dep_got.status == STATUS_BLOCKED
    assert dep_got.payload.get("block_kind") == "dep_failed"
    events = [e for e in board.events(dependent.id) if e["kind"] == "dep_failed"]
    assert len(events) == 1
    assert events[0]["payload"]["prerequisite"] == prereq.id


def test_ageout_provider_outage_requeue_does_not_cascade_dep_failed(clocked_board):
    """Sanity check on the OTHER branch: a REQUEUED (not cancelled)
    provider_outage prerequisite must NOT cascade dep_failed to its
    dependent — it isn't terminal-bad, it just went back to 'ready'."""
    board, clock = clocked_board
    prereq = board.create(user_id="u1", title="prereq", max_retries=5)
    dependent = board.create(user_id="u1", title="dependent", depends_on=[prereq.id])
    assert dependent.status == STATUS_WAITING

    board.block_from_ready(prereq.id, error="transient provider blip")
    board.stamp_block_kind(prereq.id, "provider_outage")
    assert board.get(dependent.id).status == STATUS_WAITING  # untouched so far

    clock.advance(31 * 60)  # past the short retry window
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 1
    assert board.get(prereq.id).status == STATUS_READY  # requeued, not cancelled

    dep_got = board.get(dependent.id)
    assert dep_got.status == STATUS_WAITING  # untouched — prereq isn't dead, just retrying
    assert "dep_failed" not in [e["kind"] for e in board.events(dependent.id)]


# ---------------------------------------------------------------------------
# flag registry (both regenerated mirrors)
# ---------------------------------------------------------------------------

def test_flag_row_present_in_generated_catalog():
    from core.flags_catalog import CATALOG
    names = {row[0] for row in CATALOG}
    assert "GOAL_BLOCKED_PROVIDER_RETRY_MIN" in names


def test_flag_resolves_documented_default_via_registry():
    from core.flags import resolve_flag
    r = resolve_flag("GOAL_BLOCKED_PROVIDER_RETRY_MIN", {})
    assert r.value == 30


def test_provider_exhausted_event_gated_on_cas_success(clocked_board):
    """T2.1 review finding #2 minor: the provider_retry_exhausted event is
    only fired when the CAS UPDATE succeeds (rc==1). If the row's status
    changes from 'blocked' to something else between the read and write
    (e.g. owner cancels), the UPDATE hits 0 rows and the event must NOT fire."""
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="provider_outage")

    # Loop through 16 requeues to hit the cap
    for i in range(16):
        clock.advance(31 * 60)
        n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
        assert n == 1
        board.claim(g.id, "w1", ttl_seconds=900)
        board.record_failure(g.id, error="boom")

    # 17th tick: before age_out_blocked tries to stamp exhausted, cancel the goal
    # This simulates the row being moved off 'blocked' between the SELECT and UPDATE
    clock.advance(31 * 60)
    assert board.get(g.id).status == STATUS_BLOCKED
    board.cancel(g.id, user_id="u1")
    assert board.get(g.id).status == STATUS_CANCELLED

    # Now call age_out_blocked; the row won't match the WHERE clause anymore
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 0  # no transition occurred

    # Verify no provider_retry_exhausted event was fired
    events = [e["kind"] for e in board.events(g.id)]
    assert "provider_retry_exhausted" not in events


# ---------------------------------------------------------------------------
# T2.1 final-review Fix 1: owner unblock clears the stale block_kind so the
# NEXT block episode gets a fresh classification (the choke point's
# only_if_absent guard would otherwise keep the OLD kind forever).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unblock_clears_stale_needs_input_so_provider_outage_self_heal_engages(
        board, dispatcher):
    g = board.create(user_id="u1", title="flaky", max_retries=1)
    board.claim(g.id, "w1", ttl_seconds=900)
    await dispatcher._fail_run(g, None, error="ordinary failure, nothing to do with providers")
    got = board.get(g.id)
    assert got.status == STATUS_BLOCKED
    assert got.payload.get("block_kind") == "needs_input"

    assert board.unblock(g.id, user_id="u1", rationale="owner reset") is True
    reset = board.get(g.id)
    assert reset.status == STATUS_READY
    assert "block_kind" not in reset.payload

    board.claim(g.id, "w1", ttl_seconds=900)
    await dispatcher._fail_run(
        reset, None, error="llm_provider_exhausted: ALL LLM PROVIDERS EXHAUSTED")
    reblocked = board.get(g.id)
    assert reblocked.status == STATUS_BLOCKED
    # A stale needs_input surviving the unblock would have refused this
    # stamp (only_if_absent) and left the genuine provider death mislabeled
    # needs_input — which kind-aware aging never auto-heals.
    assert reblocked.payload.get("block_kind") == "provider_outage"


def test_unblock_preserves_provider_requeues_ledger_across_the_episode(clocked_board):
    board, clock = clocked_board
    g = _block_with_kind(board, block_kind="provider_outage")
    clock.advance(31 * 60)
    n = board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert n == 1
    requeued = board.get(g.id)
    assert requeued.status == STATUS_READY
    assert requeued.payload.get("provider_requeues") == 1

    # re-trip the breaker (block_kind + the requeue ledger persist across the
    # payload merge-write, same as the requeue-cap truth-table test above)
    board.claim(g.id, "w1", ttl_seconds=900)
    board.record_failure(g.id, error="boom")
    tripped = board.get(g.id)
    assert tripped.status == STATUS_BLOCKED
    assert tripped.payload.get("block_kind") == "provider_outage"
    assert tripped.payload.get("provider_requeues") == 1

    assert board.unblock(g.id, user_id="u1", rationale="owner reset") is True
    got = board.get(g.id)
    assert got.status == STATUS_READY
    assert "block_kind" not in got.payload
    # the requeue-cap ledger key itself must SURVIVE the reset — only the
    # discriminator is cleared, per the finding's explicit preservation clause.
    assert got.payload.get("provider_requeues") == 1


# ---------------------------------------------------------------------------
# 2026-07-23 validation (P3): payload-only stamps must be single-shot under
# concurrent sweeps. The maintenance sweeps run BEFORE the TickLock, so two
# workers can interleave SELECT-then-UPDATE; WHERE status='blocked' alone gave
# both writers rc=1 (the UPDATE changes no status) and double-emitted the
# audit event. The stored-payload predicate must reject the stale loser.
# ---------------------------------------------------------------------------

def test_exhausted_stamp_rejects_stale_concurrent_writer(board):
    import json as _json
    from core.sqlite_util import execute_retry

    g = _block_with_kind(board, block_kind="provider_outage")
    stale = dict(board.get(g.id).payload or {})
    stale["provider_retry_exhausted"] = True
    sql = """UPDATE goals SET payload=? WHERE id=? AND status='blocked'
               AND json_extract(payload,'$.provider_retry_exhausted') IS NOT 1"""
    assert execute_retry(board.db_path, sql, (_json.dumps(stale), g.id)) == 1
    # writer B computed the same stamp from a pre-stamp read: must be rejected
    assert execute_retry(board.db_path, sql, (_json.dumps(stale), g.id)) == 0


def test_stamp_block_kind_rejects_stale_same_kind_writer(board):
    import json as _json
    from core.sqlite_util import execute_retry

    g = _block_with_kind(board)
    assert board.stamp_block_kind(g.id, "needs_input") is True
    stale = dict(board.get(g.id).payload or {})
    sql = """UPDATE goals SET payload=? WHERE id=? AND status='blocked'
               AND json_extract(payload,'$.block_kind') IS NOT ?"""
    rc = execute_retry(board.db_path, sql,
                       (_json.dumps(stale), g.id, "needs_input"))
    assert rc == 0, "same-kind stale writer must be rejected by the predicate"
    kinds = [e["kind"] for e in board.events(g.id)]
    assert kinds.count("block_kind_set") == 1
