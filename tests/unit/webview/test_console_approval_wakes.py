"""043 W10 — a console approval wakes the owning session across processes.

On prod Rob #1 the console (``polyrob-webview.service``) and the agent
(``polyrob.service``) run as SEPARATE processes. When the owner approves a gated
tool call from the console, the deciding process holds NO ``task_agent`` that owns
the asking session — and ``deliver_self_wake`` refuses a remote session by design.

The honest shape (``tools/controller/approval_queue.py::decide_tool_approval`` +
``core/wake_queue.py`` + the ``core/autonomy_runtime.py`` wake-drain tick):
  * a cross-process approval writes a DURABLE wake row for the owning process;
  * a same-process approval (the agent owns the session, resident here) wakes it
    in-process, no durable row;
  * a GOAL-BLOCKING approval writes NO wake either way (the carve-out — the
    re-armed goal + dispatcher redeem the grant; a forged wake turn may not spend);
  * the ask row flips regardless of whether any wake is delivered.

Isolation: the wake queue lives beside the board's ``goals.db``, so a tmp board
keeps every wake in the tmp data home — no env seam.
"""
import asyncio

import pytest

from agents.task.goals.board import (
    ASK_FULFILLED, ASK_REJECTED, STATUS_READY, GoalBoard)
from agents.task.session_route import LOCAL, MISSING, REMOTE, SessionRoute
from tools.controller.approval_queue import (
    _wake_queue_for_board, decide_tool_approval, tap_display_id)


@pytest.fixture(autouse=True)
def _clean_wake_singleton():
    """The wake queue is a per-path singleton — clear the cache each test so a
    stale instance never leaks across tmp data homes."""
    import core.wake_queue as wq
    wq._INSTANCES.clear()
    yield
    wq._INSTANCES.clear()


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _wake_queue(board):
    """The SAME queue the producer resolves (beside the board's goals.db)."""
    return _wake_queue_for_board(board)


def _tool_ask(board, *, session_id="s1", tool="x402_request", blocks=None):
    return board.create_ask(
        user_id="u1", what=f"Approve {tool}?", why="x", force=True,
        blocks_goal_ids=blocks or [],
        extra_payload={"ask_kind": "tool_approval", "tool_name": tool,
                       "session_id": session_id})


class _Agent:
    """A minimal TaskAgent stand-in. ``route_session`` decides local (owns the
    session) vs remote/missing (does not); ``deliver_self_wake`` records
    in-process wakes."""

    def __init__(self, route_status=LOCAL):
        self._route_status = route_status
        self.woken = []

    def route_session(self, session_id):
        if self._route_status == LOCAL:
            return SessionRoute(status=LOCAL, orchestrator=object())
        if self._route_status == REMOTE:
            return SessionRoute(status=REMOTE, owner_pid=99999)
        return SessionRoute(status=MISSING)

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.woken.append((session_id, user_id, text, metadata))
        return True


# --- the producer: what a console decision writes -------------------------------

def test_cross_process_console_approval_writes_a_durable_wake_row(board):
    """A console with NO in-process agent (prod: the agent is a separate service)
    enqueues a durable wake row for the owning process, and the ask still flips."""
    ask = _tool_ask(board, session_id="sX")
    ok, _ = decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                                 approved=True, task_agent=None)
    assert ok is True
    assert board.get(ask.id).status == ASK_FULFILLED  # (d) the ask flips regardless
    q = _wake_queue(board)
    assert q.pending_count() == 1
    rows = q.claim_pending("pid:test")
    assert len(rows) == 1
    assert rows[0].session_id == "sX"
    assert "grant is live" in rows[0].text
    assert "Do not retry in a loop" in rows[0].text
    assert rows[0].metadata.get("kind") == "approval_granted"


def test_a_monitoring_console_agent_does_not_wake_in_process(board):
    """The prod console holds a TaskAgent that does NOT own the session (route
    REMOTE) — presence is not ownership. It must write the durable row, never
    recreate the session in the console process."""
    agent = _Agent(route_status=REMOTE)
    ask = _tool_ask(board, session_id="sR")
    ok, _ = decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                                 approved=True, task_agent=agent)
    assert ok is True
    assert agent.woken == []                          # never claimed the remote session
    assert _wake_queue(board).pending_count() == 1    # durable row for the owner


def test_a_goal_blocking_approval_writes_NO_wake(board):
    """The carve-out: an ask that re-arms a goal writes no wake (in ANY process) —
    the dispatcher redeems the grant; a forged wake turn cannot spend."""
    goal = board.create(user_id="u1", title="bridge", status=STATUS_READY)
    board.record_failure(goal.id, error="blocked on approval")
    board.update_status(goal.id, "blocked")
    ask = _tool_ask(board, session_id="sG", tool="defi_trade_bridge",
                    blocks=[goal.id])
    ok, _ = decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                                 approved=True, task_agent=None)
    assert ok is True
    assert board.get(ask.id).status == ASK_FULFILLED   # the ask still flips
    assert board.get(goal.id).status == STATUS_READY   # the goal is re-armed
    assert _wake_queue(board).pending_count() == 0     # the carve-out: NO wake row


def test_a_rejection_writes_no_wake(board):
    ask = _tool_ask(board, session_id="sN")
    ok, _ = decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                                 approved=False, task_agent=None)
    assert ok is True
    assert board.get(ask.id).status == ASK_REJECTED
    assert _wake_queue(board).pending_count() == 0


@pytest.mark.asyncio
async def test_same_process_owner_wakes_in_process_no_durable_row(board):
    """When the deciding process OWNS the session (route LOCAL), the wake fires
    in-process and no durable row is written."""
    agent = _Agent(route_status=LOCAL)
    ask = _tool_ask(board, session_id="sL")
    ok, _ = decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                                 approved=True, task_agent=agent)
    assert ok is True
    await asyncio.sleep(0)   # the in-process wake is fire-and-forget
    await asyncio.sleep(0)
    assert len(agent.woken) == 1 and agent.woken[0][0] == "sL"
    assert _wake_queue(board).pending_count() == 0


# --- the consumer: the owning process drains the durable row --------------------

@pytest.mark.asyncio
async def test_the_owning_process_tick_drains_and_delivers_the_wake(board, tmp_path):
    """End-to-end: a cross-process approval enqueues a row; the OWNING process's
    wake-drain tick claims it and delivers it in-process via deliver_self_wake."""
    from core.autonomy_runtime import _build_wake_drain_ticker
    ask = _tool_ask(board, session_id="sD")
    decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                         approved=True, task_agent=None)
    assert _wake_queue(board).pending_count() == 1

    agent = _Agent(route_status=LOCAL)   # the owning process resolves it locally
    # data_dir is the board's dir, so the drain resolves the SAME wakes.db.
    ticker = _build_wake_drain_ticker(agent, str(tmp_path))
    await ticker.tick_coro()

    assert len(agent.woken) == 1 and agent.woken[0][0] == "sD"
    assert _wake_queue(board).pending_count() == 0   # delivered, not left pending


@pytest.mark.asyncio
async def test_an_undeliverable_wake_is_dropped_after_the_attempt_cap(board, tmp_path):
    """A wake that never delivers (self-wake off / session gone / paused) is
    retried then DROPPED — the grant is durable, so a lost nudge is safe. It is
    never retried forever."""
    from core.autonomy_runtime import _build_wake_drain_ticker
    from core.wake_queue import WAKE_MAX_ATTEMPTS

    class _NeverDelivers(_Agent):
        async def deliver_self_wake(self, *a, **k):
            return False

    ask = _tool_ask(board, session_id="sF")
    decide_tool_approval(board, tap_display_id(ask.id), user_id="u1",
                         approved=True, task_agent=None)
    wake_id = _wake_queue(board).claim_pending("peek")[0].id
    _wake_queue(board).fail(wake_id)   # put it back for the drain to claim

    ticker = _build_wake_drain_ticker(_NeverDelivers(route_status=LOCAL),
                                      str(tmp_path))
    for _ in range(WAKE_MAX_ATTEMPTS + 2):
        await ticker.tick_coro()

    assert _wake_queue(board).pending_count() == 0
    assert _wake_queue(board).status_of(wake_id) == "dropped"
