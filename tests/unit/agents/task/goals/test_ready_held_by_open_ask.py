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
    held = board.create(user_id="rob", title="Bridge 0.05 ETH to RH")
    free = board.create(user_id="rob", title="Write the weekly recap")
    ask = board.create_ask(user_id="rob", what="Approve the bridge?", blocks_goal_ids=[held.id])
    assert board.get(ask.id).status == ASK_OPEN
    assert board.get(held.id).status == STATUS_READY, "the goal is not BLOCKED, it is held"

    assert _ids(board.ready(limit=10)) == [free.id]
    assert _ids(board.ready(limit=10, yield_ageing=2)) == [free.id]
    assert _ids(board.ready_fair(limit=10)) == [free.id]


def test_hold_is_recorded_once_per_goal_and_ask(board):
    held = board.create(user_id="rob", title="Bridge")
    ask = board.create_ask(user_id="rob", what="Approve?", blocks_goal_ids=[held.id])
    for _ in range(3):
        board.ready(limit=10)
    evs = [e for e in board.events(held.id) if e["kind"] == "held_by_ask"]
    assert len(evs) == 1
    assert evs[0]["payload"]["ask_id"] == ask.id


def test_approval_releases_the_hold_and_the_goal_is_served_again(board):
    held = board.create(user_id="rob", title="Bridge")
    ask = board.create_ask(user_id="rob", what="Approve?", blocks_goal_ids=[held.id])
    assert _ids(board.ready(limit=10)) == []
    board.decide_ask(ask.id, user_id="rob", approved=True)
    assert _ids(board.ready(limit=10)) == [held.id]


def test_rejection_also_releases_the_hold(board):
    """A rejected ask is no longer OPEN, so the hold lifts; what the goal does next
    (fail again, then trip its breaker) is the existing retry contract, unchanged."""
    held = board.create(user_id="rob", title="Bridge")
    ask = board.create_ask(user_id="rob", what="Approve?", blocks_goal_ids=[held.id])
    board.decide_ask(ask.id, user_id="rob", approved=False)
    assert _ids(board.ready(limit=10)) == [held.id]


def test_an_ask_naming_no_goal_holds_nothing(board):
    g = board.create(user_id="rob", title="Recap")
    board.create_ask(user_id="rob", what="Top up OpenRouter")
    assert _ids(board.ready(limit=10)) == [g.id]


def test_a_hold_is_tenant_exact(board):
    """An ask names goal IDS, which are unique across tenants — another tenant's
    ask can never hold this tenant's goal by accident."""
    g = board.create(user_id="rob", title="Recap")
    board.create_ask(user_id="someone_else", what="Approve?", blocks_goal_ids=["not-a-goal"])
    assert _ids(board.ready(limit=10)) == [g.id]


def test_hold_fails_open_when_the_ask_read_raises(board, monkeypatch):
    g = board.create(user_id="rob", title="Recap")

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(board, "_held_by_open_ask", _boom)
    assert _ids(board.ready(limit=10)) == [g.id]
