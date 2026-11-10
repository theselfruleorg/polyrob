"""A ready goal that an OPEN owner ask names is HELD, not re-served.

Intel inbox 2026-09-16 (evidence 2026-09-12, episodes 1162–1168): a goal waiting on the
owner's tap-approval for a bridge was dispatched seven more times in 75 min (~$0.42,
one overclaim the judge had to reject, owner noise) — every run ended the same way,
because `owner_queue` names the goal in the ask's ``blocks_goal_ids`` and approval
RE-ARMS it, but nothing held it while the ask was open: a failed run puts the goal
straight back to ``ready``. ``ready()`` now skips a goal any open ask names, records
ONE ``held_by_ask`` event per (goal, ask), and the existing approve/reject hops are
untouched — approval still re-arms, rejection still leaves it alone.
"""
import pytest

from agents.task.goals.board import ASK_OPEN, GoalBoard, STATUS_READY


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


def _ids(goals):
    return [g.id for g in goals]


def test_ready_skips_a_goal_named_by_an_open_ask(board):
    held = board.create(user_id="alice", title="Bridge 0.05 ETH to RH")
    free = board.create(user_id="alice", title="Write the weekly recap")
    ask = board.create_ask(user_id="alice", what="Approve the bridge?", blocks_goal_ids=[held.id])
    assert board.get(ask.id).status == ASK_OPEN
    assert board.get(held.id).status == STATUS_READY, "the goal is not BLOCKED, it is held"

    assert _ids(board.ready(limit=10)) == [free.id]
    assert _ids(board.ready(limit=10, yield_ageing=2)) == [free.id]


def test_hold_is_recorded_once_per_goal_and_ask(board):
    held = board.create(user_id="alice", title="Bridge")
    ask = board.create_ask(user_id="alice", what="Approve?", blocks_goal_ids=[held.id])
    for _ in range(3):
        board.ready(limit=10)
    evs = [e for e in board.events(held.id) if e["kind"] == "held_by_ask"]
    assert len(evs) == 1
    assert evs[0]["payload"]["ask_id"] == ask.id


def test_approval_releases_the_hold_and_the_goal_is_served_again(board):
    held = board.create(user_id="alice", title="Bridge")
    ask = board.create_ask(user_id="alice", what="Approve?", blocks_goal_ids=[held.id])
    assert _ids(board.ready(limit=10)) == []
    board.decide_ask(ask.id, user_id="alice", approved=True)
    assert _ids(board.ready(limit=10)) == [held.id]


def test_rejection_also_releases_the_hold(board):
    """A rejected ask is no longer OPEN, so the hold lifts; what the goal does next
    (fail again, then trip its breaker) is the existing retry contract, unchanged."""
    held = board.create(user_id="alice", title="Bridge")
    ask = board.create_ask(user_id="alice", what="Approve?", blocks_goal_ids=[held.id])
    board.decide_ask(ask.id, user_id="alice", approved=False)
    assert _ids(board.ready(limit=10)) == [held.id]


def test_an_ask_naming_no_goal_holds_nothing(board):
    g = board.create(user_id="alice", title="Recap")
    board.create_ask(user_id="alice", what="Top up OpenRouter")
    assert _ids(board.ready(limit=10)) == [g.id]


def test_a_hold_is_tenant_exact(board):
    """An ask names goal IDS, which are unique across tenants — another tenant's
    ask can never hold this tenant's goal by accident."""
    g = board.create(user_id="alice", title="Recap")
    board.create_ask(user_id="someone_else", what="Approve?", blocks_goal_ids=["not-a-goal"])
    assert _ids(board.ready(limit=10)) == [g.id]


def test_hold_fails_closed_when_the_ask_read_raises(board, monkeypatch, caplog):
    # Before: an unreadable ask set held nothing, so a goal an open ask names was
    # served again into the same owner decision. Now nothing is served that tick.
    import logging
    g = board.create(user_id="alice", title="Recap")

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(board, "_held_by_open_ask", _boom)
    with caplog.at_level(logging.WARNING):
        assert _ids(board.ready(limit=10)) == []
    assert "hold-by-ask read failed" in caplog.text
    monkeypatch.undo()
    assert _ids(board.ready(limit=10)) == [g.id]


def _set_payload(board, ask_id, raw):
    import sqlite3
    con = sqlite3.connect(board.db_path)
    con.execute("UPDATE goals SET payload=? WHERE id=?", (raw, ask_id))
    con.commit()
    con.close()


@pytest.mark.parametrize("raw", [
    '{"blocks_goal_ids": 5}',
    '["blocks_goal_ids"]',
    '"blocks_goal_ids"',
    '{"blocks_goal_ids": "abc"}',
])
def test_one_malformed_ask_row_holds_nothing_and_freezes_no_goal(board, raw, caplog):
    # Before: a bad row of ANY tenant raised outside the per-row try, the read
    # failed closed on every tick and every goal of every tenant froze.
    import logging
    held = board.create(user_id="alice", title="Bridge")
    free = board.create(user_id="alice", title="Recap")
    board.create_ask(user_id="alice", what="Approve?", blocks_goal_ids=[held.id])
    bad = board.create_ask(user_id="bob", what="Odd", blocks_goal_ids=["x"])
    _set_payload(board, bad.id, raw)
    with caplog.at_level(logging.WARNING):
        assert _ids(board.ready(limit=10)) == [free.id]
    assert bad.id in caplog.text
    assert "hold-by-ask read failed" not in caplog.text


def test_a_failed_ask_read_is_recorded_for_status_once_per_window(board, monkeypatch):
    from agents.task.goals import board_asks
    from core.event_kinds import GOAL_HOLD_UNREADABLE
    seen = []
    monkeypatch.setattr(board_asks, "_emit_event",
                        lambda kind, **kw: seen.append((kind, kw)), raising=False)
    monkeypatch.setattr(board_asks, "_last_unreadable_emit", [0.0], raising=False)
    board.create(user_id="alice", title="Recap")

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(board, "_held_by_open_ask", _boom)
    for _ in range(3):
        assert _ids(board.ready(limit=10)) == []
    assert len(seen) == 1
    kind, kw = seen[0]
    assert kind == GOAL_HOLD_UNREADABLE
    assert "db gone" in kw["attrs"]["error"]
    assert kw["attrs"]["ready"] == 1
