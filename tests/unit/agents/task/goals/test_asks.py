"""First-class asks (§7.2b): a durable "I need something from the owner" object.

An ask is a ``kind='ask'`` row on the existing goals table (no new store). It is
never dispatched, dedups against OPEN asks only, and fulfilling it flips its
blocked dependent goals back to ``ready`` with a clean failure counter — the
unblock hop that turns a fulfilled ask into resumed work.
"""
import asyncio

import pytest

from agents.task.goals.board import (
    ASK_FULFILLED, ASK_OPEN, GoalBoard, KIND_ASK, STATUS_BLOCKED, STATUS_READY,
)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


def _block(board, goal):
    board.claim(goal.id, "w", ttl_seconds=60)
    board.record_failure(goal.id, error="needs twitter write access")
    board.claim(goal.id, "w", ttl_seconds=60)
    board.record_failure(goal.id, error="needs twitter write access")  # trips breaker


def test_create_ask_basic(board):
    a = board.create_ask(user_id="rob", what="Grant Twitter write access",
                         why="X-growth objective needs twitter_post")
    assert a.kind == KIND_ASK
    assert a.status == ASK_OPEN
    assert a.title == "Grant Twitter write access"
    assert board.get(a.id).body == "X-growth objective needs twitter_post"


def test_asks_never_dispatch(board):
    board.create_ask(user_id="rob", what="Grant Twitter write access")
    assert board.ready(limit=10) == []


def test_create_ask_dedups_open_asks(board):
    a1 = board.create_ask(user_id="rob", what="Grant Twitter write access")
    a2 = board.create_ask(user_id="rob", what="Grant twitter WRITE access!")
    assert a2.id == a1.id  # refreshed, not duplicated
    assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 1


def test_create_ask_does_not_dedup_against_goals(board):
    g = board.create(user_id="rob", title="Grant Twitter write access")
    a = board.create_ask(user_id="rob", what="Grant Twitter write access")
    assert a.id != g.id
    assert a.kind == KIND_ASK


def test_goal_creation_does_not_dedup_against_asks(board):
    # An auto-created ask ("Unblock goal: X") must never block the planner from
    # (re)creating a similar goal — dedup for goals excludes kind='ask'.
    board.create_ask(user_id="rob", what="Post the OSS launch announcement thread")
    g = board.create(user_id="rob", title="Post the OSS launch announcement thread")
    assert g.kind == "goal"


def test_fulfill_ask_unblocks_dependents(board):
    g = board.create(user_id="rob", title="Post the OSS launch announcement thread")
    _block(board, g)
    assert board.get(g.id).status == STATUS_BLOCKED
    a = board.create_ask(user_id="rob", what="Grant Twitter write access",
                         blocks_goal_ids=[g.id])
    ok, unblocked = board.fulfill_ask(a.id, user_id="rob")
    assert ok is True and unblocked == 1
    refreshed = board.get(g.id)
    assert refreshed.status == STATUS_READY
    assert refreshed.consecutive_failures == 0
    assert board.get(a.id).status == ASK_FULFILLED


def test_fulfill_ask_stamps_owner_unblocked_on_goal(board):
    """2026-07-14 night-2: without a fulfillment stamp the retry prompt shows only
    the old failure ledger and the agent gives up from memory without retrying."""
    g = board.create(user_id="rob", title="Post the intro in the public group")
    _block(board, g)
    a = board.create_ask(user_id="rob", what="Grant telegram posting",
                         blocks_goal_ids=[g.id])
    ok, unblocked = board.fulfill_ask(a.id, user_id="rob")
    assert ok is True and unblocked == 1
    payload = board.get(g.id).payload or {}
    stamp = payload.get("owner_unblocked") or {}
    assert stamp.get("ask_id") == a.id
    assert float(stamp.get("ts") or 0) > 0


def test_fulfill_ask_wrong_tenant_noop(board):
    a = board.create_ask(user_id="rob", what="Grant Twitter write access")
    ok, unblocked = board.fulfill_ask(a.id, user_id="mallory")
    assert ok is False and unblocked == 0
    assert board.get(a.id).status == ASK_OPEN


def test_fulfill_ask_skips_non_blocked_dependents(board):
    g = board.create(user_id="rob", title="A goal that is still ready entirely")
    a = board.create_ask(user_id="rob", what="Something", blocks_goal_ids=[g.id])
    ok, unblocked = board.fulfill_ask(a.id, user_id="rob")
    assert ok is True and unblocked == 0  # ready goal untouched
    assert board.get(g.id).status == STATUS_READY


# --- producers: escalation paths also leave a tracked ask ---------------------

class _Sink:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _Container:
    def __init__(self, sink):
        self._sink = sink

    def get_service(self, name):
        return self._sink if name in ("telegram_sink", "message_router") else None


class _Agent:
    def __init__(self, sink):
        self.container = _Container(sink)


def test_blocked_escalation_creates_ask(board, monkeypatch):
    from agents.task.goals.dispatcher import GoalDispatcher
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "28436760")
    g = board.create(user_id="rob", title="Post the OSS launch announcement thread")
    _block(board, g)
    d = GoalDispatcher(board, _Agent(_Sink()))
    asyncio.run(d._maybe_escalate_blocked(board.get(g.id)))
    asks = board.asks(user_id="rob", status=ASK_OPEN)
    assert len(asks) == 1
    assert g.id in (asks[0].payload or {}).get("blocks_goal_ids", [])


def test_blocked_escalation_ask_is_durable_even_when_push_flag_off(board, monkeypatch):
    # T2-03/T4-04: the ask row was gated on the SAME flag as the owner push, so with
    # the default OFF a blocked goal left NO ask and `owner fulfill` had nothing to
    # consume (the need evaporated). The durable ask must now be created regardless of
    # the push flag — only the PUSH is suppressed under the silent posture.
    from agents.task.goals.dispatcher import GoalDispatcher
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "false")
    monkeypatch.delenv("AUTONOMY_POSTURE", raising=False)
    sink = _Sink()
    g = board.create(user_id="rob", title="Post the OSS launch announcement thread")
    _block(board, g)
    d = GoalDispatcher(board, _Agent(sink))
    asyncio.run(d._maybe_escalate_blocked(board.get(g.id)))
    asks = board.asks(user_id="rob", status=ASK_OPEN)
    assert len(asks) == 1, "the ask must survive even when the owner push is off"
    assert g.id in (asks[0].payload or {}).get("blocks_goal_ids", [])
    assert sink.sent == [], "the push itself must stay suppressed under the flag/silent posture"
