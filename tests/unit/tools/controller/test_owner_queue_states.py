"""098 TS-14: owner ask/grant — decide keeps other keys, asks expire, one late send."""
import time

import pytest

from agents.task.goals.board import ASK_OBSOLETE, ASK_OPEN, GoalBoard
from core.sqlite_util import execute_retry
from core.wake_queue import get_wake_queue
from tools.controller import approval_queue as aq
from tools.controller.approval_ask_ttl import ask_ttl_hours, grant_deadline


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _outbound_ask(board, **extra):
    return board.create_ask(user_id="u1", what="Approve post? [h]", force=True,
                            extra_payload={"ask_kind": "tool_approval", "tool_name": "post",
                                           "outbound_write": True, "grant_consumed": False,
                                           "session_id": "s1", **extra})


def _wakes(board):
    q = aq._wake_queue_for_board(board)
    return execute_retry(q.db_path, "SELECT id, kind FROM session_wakes", (), fetch="all") or []


def test_decide_ask_preserves_poller_gone(board, monkeypatch):
    ask = _outbound_ask(board)
    stale = board.get(ask.id)
    real_get = board.get
    calls = {"n": 0}

    def racing_get(ask_id, **kw):
        # decide_tool_approval's own read sees the row before the poller left;
        # the poller marks itself gone while the owner decides.
        calls["n"] += 1
        if calls["n"] == 1:
            aq._poller_gone(board, ask.id)
            return stale
        return real_get(ask_id, **kw)

    monkeypatch.setattr(board, "get", racing_get)
    ok, msg = aq.decide_tool_approval(board, aq.tap_display_id(ask.id), user_id="u1",
                                      approved=True)
    monkeypatch.undo()
    assert ok
    assert board.get(ask.id).payload["poller_gone"] is True
    assert len(_wakes(board)) == 1
    assert "sent once" in msg


def test_late_send_enqueued_once_when_both_writers_see_approved(board):
    ask = _outbound_ask(board)
    board.decide_ask(ask.id, user_id="u1", approved=True)
    aq._poller_gone(board, ask.id)         # writer 1 sees approved
    aq._enqueue_late_send(board, ask.id, "u1", "s1")  # writer 2 sees it too
    assert len(_wakes(board)) == 1


def test_open_tool_approval_ask_expires(board):
    ask = _outbound_ask(board, expires_at=time.time() - 1)
    keep = _outbound_ask(board, expires_at=time.time() + 3600)
    assert board.expire_open_asks() == 1
    row = board.get(ask.id)
    assert row.status == ASK_OBSOLETE and row.payload["obsolete_reason"] == "expired"
    assert board.get(keep.id).status == ASK_OPEN


def test_expired_ask_cannot_be_approved(board):
    ask = _outbound_ask(board, expires_at=time.time() - 1)
    ok, _ = board.decide_ask(ask.id, user_id="u1", approved=True)
    assert ok is False
    ok, msg = aq.decide_tool_approval(board, aq.tap_display_id(ask.id), user_id="u1",
                                      approved=True)
    assert ok is False and "expired — ask again" in msg
    assert board.get(ask.id).status == ASK_OBSOLETE
    # a tap after the sweep closed it says the same
    ok, msg = aq.decide_tool_approval(board, aq.tap_display_id(ask.id), user_id="u1",
                                      approved=True)
    assert ok is False and "expired" in msg


def test_grant_ttl_capped_by_request_age():
    now = 1_000_000.0
    # decided long after the ask's own clock ran out: the grant is capped
    d = grant_deadline(created_at=now, completed_at=now + 100 * 3600, grant_ttl_hours=24,
                       ask_expires_at=now + 24 * 3600)
    assert d == now + 48 * 3600
    d = grant_deadline(created_at=now, completed_at=now + 3600, grant_ttl_hours=24,
                       ask_expires_at=now + 72 * 3600)
    assert d == now + 25 * 3600
    assert grant_deadline(created_at=now, completed_at=None, grant_ttl_hours=1,
                          ask_expires_at=None) is None


def test_ask_ttl_setting(monkeypatch):
    monkeypatch.delenv("APPROVAL_ASK_TTL_HOURS", raising=False)
    assert ask_ttl_hours(money=False) == 72 and ask_ttl_hours(money=True) == 24
    monkeypatch.setenv("APPROVAL_ASK_TTL_HOURS", "6")
    assert ask_ttl_hours(money=False) == 6 and ask_ttl_hours(money=True) == 6


def test_new_ask_carries_expires_at(board):
    exp = aq._ask_expires_at("defi_trade")          # a money verb: 24 h
    assert time.time() + 23 * 3600 < exp <= time.time() + 24 * 3600 + 1
    exp = aq._ask_expires_at("post_tweet")          # not money: 72 h
    assert time.time() + 71 * 3600 < exp <= time.time() + 72 * 3600 + 1


def test_wake_dedup_key_one_row(tmp_path):
    q = get_wake_queue(str(tmp_path / "w.db"))
    a = q.enqueue("s", "u", "t", dedup_key="k")
    b = q.enqueue("s", "u", "t", dedup_key="k")
    c = q.enqueue("s", "u", "t")
    assert a == b and c != a and q.pending_count() == 2
