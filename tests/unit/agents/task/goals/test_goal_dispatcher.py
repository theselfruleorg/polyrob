"""W4 — goal dispatcher: gating, claim→run→record, self-wake feed."""
import asyncio

import pytest

from agents.task.goals.board import (
    GoalBoard, STATUS_DONE, STATUS_READY, STATUS_BLOCKED, STATUS_WAITING,
)
from agents.task.goals.dispatcher import GoalDispatcher


class _FakeAgent:
    def __init__(self, final="result", fail=False):
        self.final = final
        self.fail = fail
        self.ran = []
        self.woke = []
        self.requests = []  # captured create_session requests (tool-inheritance tests)

    async def create_session(self, *, user_id, request):
        self.requests.append(request)
        return {"id": f"sess-{user_id}"}

    async def run_session(self, user_id, session_id):
        self.ran.append((user_id, session_id))
        if self.fail:
            raise RuntimeError("run boom")
        return self.final

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.woke.append((session_id, text, metadata))
        return True

    def get_orchestrator(self, session_id):
        return None


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


async def _wait_for_runs(dispatcher, timeout=3.0):
    """Wait for fire-and-forget goal runs without assuming runner speed."""
    tasks = tuple(dispatcher._inflight)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)


@pytest.mark.asyncio
async def test_dispatch_noop_when_disabled(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "false")
    board.create(user_id="u1", title="t")
    agent = _FakeAgent()
    d = GoalDispatcher(board, agent)
    assert await d.dispatch_once() == 0
    assert agent.ran == []


@pytest.mark.asyncio
async def test_dispatch_runs_and_completes(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")  # §4.3: wake rides verified; test the rail itself
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    # self-wake is gated OFF by default (P5 T7); enable it here to exercise the feed.
    monkeypatch.setenv("GOAL_SELF_WAKE_ENABLED", "true")
    g = board.create(user_id="u1", title="do it", body="the work",
                     payload={"origin_session_id": "chat-origin"})  # 2026-08-28: wake targets the creator, never the run session
    agent = _FakeAgent(final="done well")
    d = GoalDispatcher(board, agent)
    n = await d.dispatch_once()
    assert n == 1
    # let the fire-and-forget _run_goal task finish
    await _wait_for_runs(d)
    got = board.get(g.id)
    assert got.status == STATUS_DONE
    assert got.result == "done well"
    assert agent.ran == [("u1", "sess-u1")]
    assert agent.woke and agent.woke[0][2]["goal_id"] == g.id  # self-wake fed
    assert agent.woke[0][0] == "chat-origin"  # ...to the CREATING session, not sess-u1


@pytest.mark.asyncio
async def test_self_wake_marks_episode_surfaced(board, monkeypatch):
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")  # §4.3: wake rides verified; test the rail itself
    """Task 7: a successful self-wake delivery marks the goal's episode surfaced
    (so the session-start digest doesn't repeat what self-wake already told the
    owner). Fail-open: never blocks the self-wake feed itself.

    Also proves FIX2 (tenant scoping): the dispatcher must pass the goal's
    own user_id to mark_episode_surfaced, not just session_id.
    """
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    monkeypatch.setenv("GOAL_SELF_WAKE_ENABLED", "true")

    marked = {}

    class _FakeProvider:
        def mark_episode_surfaced(self, *, session_id, user_id=None):
            marked["session_id"] = session_id
            marked["user_id"] = user_id

    class _FakeRegistry:
        def active(self):
            return _FakeProvider()

    monkeypatch.setattr(
        "modules.memory.registry.get_memory_registry", lambda: _FakeRegistry())

    g = board.create(user_id="u1", title="do it", body="the work",
                     payload={"origin_session_id": "chat-origin"})  # 2026-08-28: wake targets the creator, never the run session
    agent = _FakeAgent(final="done well")
    d = GoalDispatcher(board, agent)
    await d.dispatch_once()
    await _wait_for_runs(d)
    assert marked["session_id"] == "sess-u1"
    assert marked["user_id"] == "u1"


@pytest.mark.asyncio
async def test_self_wake_not_marked_when_delivery_fails(board, monkeypatch):
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")  # §4.3: wake rides verified; test the rail itself
    """FIX1: deliver_self_wake returning False (SELF_WAKE_ENABLED off, a
    remote/non-resident session dropped+audited, or the reentry budget
    exhausted) must NOT mark the episode surfaced -- otherwise the
    session-start digest (exclude_surfaced=True) omits a run the owner was
    never actually told about."""
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    monkeypatch.setenv("GOAL_SELF_WAKE_ENABLED", "true")

    marked = {"called": False}

    class _FakeProvider:
        def mark_episode_surfaced(self, *, session_id, user_id=None):
            marked["called"] = True

    class _FakeRegistry:
        def active(self):
            return _FakeProvider()

    monkeypatch.setattr(
        "modules.memory.registry.get_memory_registry", lambda: _FakeRegistry())

    class _NoWakeAgent(_FakeAgent):
        async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
            self.woke.append((session_id, text, metadata))
            return False  # not delivered

    g = board.create(user_id="u1", title="do it", body="the work",
                     payload={"origin_session_id": "chat-origin"})  # 2026-08-28: wake targets the creator, never the run session
    agent = _NoWakeAgent(final="done well")
    d = GoalDispatcher(board, agent)
    await d.dispatch_once()
    await _wait_for_runs(d)
    assert agent.woke  # self-wake WAS attempted
    assert marked["called"] is False  # but NOT marked surfaced


@pytest.mark.asyncio
async def test_success_episode_not_flipped_to_failed_by_later_raise(board, monkeypatch, tmp_path):
    """FIX3 (T3-M1): finalize_episode(outcome="done") already wrote the success
    row; if a LATER statement in the same try block raises (e.g.
    extract_outcome_line), the outer except's failed-write must not overwrite
    the just-recorded success outcome with "failed"."""
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    monkeypatch.setenv("GOAL_SELF_WAKE_ENABLED", "false")

    from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
    import modules.memory.registry as reg

    provider = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(provider)

    def _boom(final):
        raise RuntimeError("post-success boom")

    monkeypatch.setattr("agents.task.goals.context.extract_outcome_line", _boom)

    try:
        g = board.create(user_id="u1", title="do it", body="the work")
        agent = _FakeAgent(final="done well")
        d = GoalDispatcher(board, agent)
        await d.dispatch_once()
        await _wait_for_runs(d)

        out = await provider.recall_episodes(user_id="u1", limit=5)
        assert len(out) == 1
        assert out[0].outcome == "done"  # NOT flipped to "failed"
    finally:
        reg.reset_memory_registry()


@pytest.mark.asyncio
async def test_dispatch_skipped_while_interactive_busy(board, monkeypatch):
    """A human mid-turn in the REPL defers goal execution (shared CWD workspace)."""
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    g = board.create(user_id="u1", title="do it", body="the work")
    agent = _FakeAgent()
    d = GoalDispatcher(board, agent)
    from core.interactive_gate import interactive_turn
    with interactive_turn():
        n = await d.dispatch_once()
    assert n == 0          # deferred, nothing dispatched
    assert agent.ran == []  # ran nothing
    assert board.get(g.id).status == STATUS_READY  # left unclaimed/queued
    # idle again -> dispatches as before
    n2 = await d.dispatch_once()
    assert n2 == 1
    await _wait_for_runs(d)
    assert board.get(g.id).status == STATUS_DONE


@pytest.mark.asyncio
async def test_dispatch_respects_concurrency_cap(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "2")
    for i in range(5):
        board.create(user_id="u1", title=f"g{i}")
    agent = _FakeAgent()
    d = GoalDispatcher(board, agent)
    n = await d.dispatch_once()
    assert n == 2  # capped


@pytest.mark.asyncio
async def test_dispatch_failure_records_breaker(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    monkeypatch.setenv("GOAL_MAX_RETRIES", "1")  # trip on first failure
    g = board.create(user_id="u1", title="flaky", max_retries=1)
    agent = _FakeAgent(fail=True)
    d = GoalDispatcher(board, agent)
    await d.dispatch_once()
    await _wait_for_runs(d)
    assert board.get(g.id).status == STATUS_BLOCKED


@pytest.mark.asyncio
async def test_child_goal_inherits_allowlisted_parent_tools(board, monkeypatch):
    """A child goal (parent_id, no payload.tools) inherits the parent's tools,
    intersected with the inheritable allowlist — money/social tools are stripped."""
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    parent = board.create(
        user_id="u1", title="parent",
        payload={"tools": ["filesystem", "browser", "anysite", "wallet", "twitter", "x402"]},
    )
    child = board.create(user_id="u1", title="child", parent_id=parent.id)
    agent = _FakeAgent()
    d = GoalDispatcher(board, agent)
    await d.dispatch_once()
    await _wait_for_runs(d)
    child_req = next(r for r in agent.requests if r["goal_id"] == child.id)
    # inherited, order-preserved, allowlist-filtered: money/social dropped
    assert child_req["tools"] == ["filesystem", "browser", "anysite"]


@pytest.mark.asyncio
async def test_root_goal_explicit_tools_unchanged(board, monkeypatch):
    """A root goal with explicit payload.tools is passed through verbatim."""
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    board.create(user_id="u1", title="root", payload={"tools": ["filesystem", "browser"]})
    agent = _FakeAgent()
    d = GoalDispatcher(board, agent)
    await d.dispatch_once()
    await _wait_for_runs(d)
    assert agent.requests[0]["tools"] == ["filesystem", "browser"]


@pytest.mark.asyncio
async def test_child_goal_explicit_tools_not_overridden(board, monkeypatch):
    """A child goal that DID set payload.tools keeps its own (no inheritance)."""
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    parent = board.create(user_id="u1", title="parent", payload={"tools": ["browser", "anysite"]})
    board.create(user_id="u1", title="child", parent_id=parent.id,
                 payload={"tools": ["filesystem"]})
    agent = _FakeAgent()
    d = GoalDispatcher(board, agent)
    await d.dispatch_once()
    await _wait_for_runs(d)
    # only the child runs as ready (parent also ready); find the child's request
    child_reqs = [r for r in agent.requests if r["tools"] == ["filesystem"]]
    assert child_reqs, agent.requests


@pytest.mark.asyncio
async def test_child_goal_no_inheritable_parent_tools_falls_back(board, monkeypatch):
    """If the parent's tools are all non-inheritable (money/social), the child falls
    back to the safe default rather than running tool-starved on a money toolset."""
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    parent = board.create(user_id="u1", title="parent", payload={"tools": ["wallet", "twitter"]})
    board.create(user_id="u1", title="child", parent_id=parent.id)
    agent = _FakeAgent()
    d = GoalDispatcher(board, agent)
    await d.dispatch_once()
    await _wait_for_runs(d)
    child_reqs = [r for r in agent.requests if r["tools"] == ["filesystem", "task"]]
    assert child_reqs, agent.requests


@pytest.mark.asyncio
async def test_inflight_caps_global_concurrency(board, monkeypatch):
    """GOAL_MAX_CONCURRENT is a GLOBAL in-flight cap, not a per-tick claim quota.

    With slow goals still running from a prior tick, a later tick must not push
    total in-flight goals past the cap.
    """
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "2")

    release = asyncio.Event()

    class _SlowAgent(_FakeAgent):
        async def run_session(self, user_id, session_id):
            self.ran.append((user_id, session_id))
            await release.wait()  # hold the goal "running"
            return self.final

    for i in range(5):
        board.create(user_id="u1", title=f"g{i}", body="work")

    agent = _SlowAgent()
    d = GoalDispatcher(board, agent)

    n1 = await d.dispatch_once()
    assert n1 == 2  # first tick fills the 2 slots
    n2 = await d.dispatch_once()
    assert n2 == 0  # both slots still in-flight → nothing new dispatched
    assert len(d._inflight) == 2

    release.set()
    await _wait_for_runs(d)
    n3 = await d.dispatch_once()
    assert n3 == 2  # slots freed → next batch


# ---------------------------------------------------------------------------
# Idle-backoff wiring (mirrors cron/runner.py's GoalTicker equivalent, Task 4).
# ---------------------------------------------------------------------------


def test_goal_ticker_backoff_wired_when_flag_enabled(monkeypatch):
    """Structural check: with the flag on, GoalTicker.run_forever still ticks
    at least once (IntervalTicker always front-loads one immediate tick before
    any interval wait, so this only proves the wiring doesn't crash -- the
    actual widen/reset behavior is proven by the two diagnostic tests below)."""
    import asyncio
    from unittest.mock import AsyncMock
    from agents.task.goals.dispatcher import GoalTicker

    monkeypatch.setenv("TICKER_IDLE_BACKOFF_ENABLED", "true")
    monkeypatch.setenv("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", "3")

    dispatcher = AsyncMock()
    dispatcher.dispatch_once = AsyncMock(return_value=0)  # 0 = idle

    async def run():
        stop = asyncio.Event()
        ticker = GoalTicker(dispatcher, interval_seconds=1)
        task = asyncio.create_task(ticker.run_forever(stop_event=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert dispatcher.dispatch_once.await_count >= 1


def test_goal_ticker_backoff_off_by_default_is_fixed_cadence(monkeypatch):
    """Without the flag, behavior is the pre-existing fixed-interval cadence
    regardless of dispatch_once's return value."""
    import asyncio
    from unittest.mock import AsyncMock
    from agents.task.goals.dispatcher import GoalTicker

    monkeypatch.delenv("TICKER_IDLE_BACKOFF_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    dispatcher = AsyncMock()
    dispatcher.dispatch_once = AsyncMock(return_value=0)

    async def run():
        stop = asyncio.Event()
        ticker = GoalTicker(dispatcher, interval_seconds=0.01)
        task = asyncio.create_task(ticker.run_forever(stop_event=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert dispatcher.dispatch_once.await_count >= 2


def test_goal_ticker_backoff_widens_interval_when_idle(monkeypatch):
    """Diagnostic proof (not just "ticked at least once"): with the flag on and
    dispatch_once staying idle (0 dispatched) every tick, the poll interval
    actually widens toward the configured cap, so far fewer ticks fire over a
    fixed window than a fixed 0.01s cadence would produce (~20 in 0.2s).
    Mirrors core/tickers.py::test_idle_backoff_grows_interval_when_inactive,
    but exercised through the real GoalTicker wiring end-to-end."""
    import asyncio
    from agents.task.goals.dispatcher import GoalTicker

    monkeypatch.setenv("TICKER_IDLE_BACKOFF_ENABLED", "true")
    monkeypatch.setenv("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", "5")

    call_times = []

    class _IdleDispatcher:
        async def dispatch_once(self):
            call_times.append(1)
            return 0  # always idle

    async def run():
        stop = asyncio.Event()
        ticker = GoalTicker(_IdleDispatcher(), interval_seconds=0.01)
        task = asyncio.create_task(ticker.run_forever(stop_event=stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    # interval_seconds=0.01, max_multiplier=5 -> cap 0.05s. Over a 0.2s window
    # this produces ~6 ticks (0.01, 0.02, 0.04, 0.05, 0.05, 0.05...) versus the
    # ~20 a fixed 0.01s cadence would fire.
    assert 3 <= len(call_times) <= 10


def test_goal_ticker_backoff_resets_on_activity(monkeypatch):
    """Diagnostic proof that activity resets the interval: a tick that reports
    dispatched > 0 resets the cadence to base, so a run with one active tick
    in the middle produces noticeably more ticks over the same window than a
    purely-idle run. Mirrors
    core/tickers.py::test_idle_backoff_resets_on_activity."""
    import asyncio
    from agents.task.goals.dispatcher import GoalTicker

    monkeypatch.setenv("TICKER_IDLE_BACKOFF_ENABLED", "true")
    monkeypatch.setenv("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", "8")

    sequence = [0, 0, 3, 0, 0]  # 3rd tick dispatches something -> resets cadence
    call_times = []

    class _MixedDispatcher:
        async def dispatch_once(self):
            idx = len(call_times)
            call_times.append(1)
            return sequence[idx] if idx < len(sequence) else 0

    async def run():
        stop = asyncio.Event()
        ticker = GoalTicker(_MixedDispatcher(), interval_seconds=0.01)
        task = asyncio.create_task(ticker.run_forever(stop_event=stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    # The activity at index 2 resets cadence back to 0.01s, so more ticks fire
    # over 0.3s than the purely-idle case above produces over 0.2s.
    assert len(call_times) >= 6


# --- T2-02: a completed goal must surface to the OWNER, not just self-wake the goal's
#     own just-finished (empty-room) session ---

@pytest.mark.asyncio
async def test_completed_goal_pushes_result_to_owner(tmp_path, monkeypatch):
    import core.self_evolution as se

    pushed = []

    async def _fake_push(container, text):
        pushed.append(text)
        return True

    monkeypatch.setattr(se, "push_owner_message", _fake_push)

    board = GoalBoard(str(tmp_path / "g.db"))

    class _Agent:
        container = object()
        deliver_self_wake = None  # isolate the owner-push path

    disp = GoalDispatcher(board, _Agent())
    goal = board.create(user_id="rob", title="Post the announcement")
    # The owner-completion push moved out of _self_wake (agent re-entry only) into
    # _notify_owner_done (decoupled, GOAL_NOTIFY_ON_DONE) — 2026-07-08.
    await disp._notify_owner_done(goal, "sess-1", "Posted the thread — 3 tweets.")

    assert pushed, "a completed goal must push its result to the owner"
    assert "Post the announcement" in pushed[0]
    assert "Posted the thread" in pushed[0]


# --- T2.1 Task 5: dispatcher e2e — a dependent goal never dispatches before
#     its prerequisite completes, and auto-dispatches the very next tick after
#     the completion sweep flips it ready. -------------------------------------

@pytest.mark.asyncio
async def test_dependent_waits_then_dispatches_after_prerequisite_completes(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_COMPLETION_JUDGE", "false")  # test the DAG rail itself, not the judge
    monkeypatch.setenv("GOAL_MAX_CONCURRENT", "5")
    monkeypatch.setenv("GOAL_SELF_WAKE_ENABLED", "false")

    prereq = board.create(user_id="u1", title="prerequisite P")
    dependent = board.create(user_id="u1", title="dependent D", depends_on=[prereq.id])

    # D was created waiting on an unresolved dep — never claimable.
    assert board.get(dependent.id).status == STATUS_WAITING

    agent = _FakeAgent(final="P done")
    d = GoalDispatcher(board, agent)

    # --- tick 1: only P is ready; D is waiting and this tick never claims it. ---
    n1 = await d.dispatch_once()
    assert n1 == 1  # exactly P claimed/dispatched this tick, never D
    await _wait_for_runs(d)

    assert agent.ran == [("u1", "sess-u1")]  # exactly one run so far: P's
    assert board.get(prereq.id).status == STATUS_DONE

    # D was never claimed/dispatched BY tick 1 itself — the only thing that
    # touched it is the completion sweep, fired as a side effect of P's own
    # run finishing (record_success -> _sweep_dependents_on_completion),
    # ahead of any second tick.
    d_kinds_after_tick1 = [e["kind"] for e in board.events(dependent.id)]
    assert "claimed" not in d_kinds_after_tick1
    assert "deps_satisfied" in d_kinds_after_tick1
    ready_after_sweep = board.get(dependent.id)
    assert ready_after_sweep.status == STATUS_READY  # flipped by the sweep
    assert ready_after_sweep.claim_lock is None       # but not yet claimed
    assert ready_after_sweep.started_at is None        # nor ever actually run

    # --- tick 2: D is now ready and gets claimed/dispatched. ---
    n2 = await d.dispatch_once()
    assert n2 == 1
    await _wait_for_runs(d)

    assert agent.ran == [("u1", "sess-u1"), ("u1", "sess-u1")]  # P then D, in order
    got_d = board.get(dependent.id)
    assert got_d.status == STATUS_DONE
    assert got_d.result == "P done"

    # D's own event order proves the claim happened strictly AFTER the
    # completion sweep unblocked it — never before (i.e. never in tick 1).
    d_kinds_final = [e["kind"] for e in board.events(dependent.id)]
    assert d_kinds_final.index("deps_satisfied") < d_kinds_final.index("claimed")
