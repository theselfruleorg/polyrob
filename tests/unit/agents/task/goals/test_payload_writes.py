"""098 AG-21: every goal/ask payload writer changes only its own keys."""
import json

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.payload_keys import append_capped
from core.goal_vocab import ASK_FULFILLED, ASK_OPEN
from core.sqlite_util import execute_retry


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _raw(board, gid):
    row = execute_retry(board.db_path, "SELECT payload FROM goals WHERE id=?", (gid,),
                        fetch="one")
    return json.loads(row["payload"])


def test_concurrent_key_updates_both_survive(board, monkeypatch):
    g = board.create(user_id="u", title="work", payload={"a": 1})
    stale = board.get(g.id)
    # writer A reads (stale), writer B lands a key, writer A writes its key
    board.merge_payload(g.id, {"b": 2})
    monkeypatch.setattr(board, "get", lambda *_a, **_k: stale)
    board.merge_payload(g.id, {"c": 3})
    board.set_outcome(g.id, "fine")
    monkeypatch.undo()
    p = _raw(board, g.id)
    assert p["a"] == 1 and p["b"] == 2 and p["c"] == 3 and p["outcome"] == "fine"


def test_widen_after_consume_keeps_grant_consumed(board, monkeypatch):
    ask = board.create_ask(user_id="u", what="Approve x?", force=True,
                           extra_payload={"grant_consumed": False, "ask_kind": "tool_approval"})
    stale = board.get(ask.id)
    ok, _ = board.decide_ask(ask.id, user_id="u", approved=True)
    assert ok and board.consume_ask_grant(ask.id)
    # the widen writer read the payload BEFORE the redeem
    monkeypatch.setattr(board, "get", lambda *_a, **_k: stale)
    board.add_ask_blocked_goals(ask.id, ["g1"])
    monkeypatch.undo()
    p = _raw(board, ask.id)
    assert p["grant_consumed"] is True
    assert board.consume_ask_grant(ask.id) is False      # never a second redeem
    assert "g1" not in (p.get("blocks_goal_ids") or [])  # a decided ask is not widened
    assert board.get(ask.id).status == ASK_FULFILLED


def test_widen_open_ask_unions(board):
    ask = board.create_ask(user_id="u", what="Need key", blocks_goal_ids=["g0"], force=True)
    assert board.add_ask_blocked_goals(ask.id, ["g1"]) == ["g0", "g1"]
    assert _raw(board, ask.id)["blocks_goal_ids"] == ["g0", "g1"]


def test_attempt_ledger_append_is_atomic(board):
    g = board.create(user_id="u", title="work", payload={"keep": True})
    for i in range(7):
        assert append_capped(board.db_path, g.id, "attempts", {"n": i}, 5)
    p = _raw(board, g.id)
    assert [a["n"] for a in p["attempts"]] == [2, 3, 4, 5, 6]
    assert p["keep"] is True


def test_attempt_append_retries_when_the_key_moved(board, monkeypatch):
    g = board.create(user_id="u", title="work")
    import agents.task.goals.payload_keys as pk
    real = pk.execute_retry
    state = {"raced": False}

    def racing(db, sql, params=(), **kw):
        # Just before the first guarded write, another writer appends.
        if sql.startswith("UPDATE goals SET payload=json_set") and not state["raced"]:
            state["raced"] = True
            real(db, "UPDATE goals SET payload=json_set(payload,'$.attempts',json(?)) "
                     "WHERE id=?", (json.dumps([{"n": "other"}]), g.id))
        return real(db, sql, params, **kw)

    monkeypatch.setattr(pk, "execute_retry", racing)
    assert append_capped(board.db_path, g.id, "attempts", {"n": "mine"}, 5)
    assert [a["n"] for a in _raw(board, g.id)["attempts"]] == ["other", "mine"]


def test_decide_ask_writes_only_its_keys(board, monkeypatch):
    ask = board.create_ask(user_id="u", what="Post this?", force=True,
                           extra_payload={"outbound_write": True})
    stale = board.get(ask.id)
    # `_poller_gone` lands between decide_ask's read and its write
    execute_retry(board.db_path, "UPDATE goals SET payload=json_set(payload,"
                  "'$.poller_gone', json('true')) WHERE id=?", (ask.id,))
    monkeypatch.setattr(board, "get", lambda *_a, **_k: stale)
    ok, _ = board.decide_ask(ask.id, user_id="u", approved=True, answer="yes")
    monkeypatch.undo()
    assert ok
    p = _raw(board, ask.id)
    assert p["poller_gone"] is True and p["decision"] == "approved" and p["answer"] == "yes"


def test_decide_ask_records_answer_on_blocked_goal_and_unblocks(board):
    g = board.create(user_id="u", title="work", payload={"block_kind": "needs_input", "x": 1})
    board.update_status(g.id, "blocked")
    ask = board.create_ask(user_id="u", what="Which key?", blocks_goal_ids=[g.id], force=True)
    ok, n = board.decide_ask(ask.id, user_id="u", approved=True, answer="use B")
    assert ok and n == 1
    row = board.get(g.id)
    assert row.status == "ready"
    assert row.payload["x"] == 1 and "block_kind" not in row.payload
    assert row.payload["owner_unblocked"]["answer"] == "use B"
    assert row.payload["owner_answers"][-1]["decision"] == "approved"
    assert board.get(ask.id).status == ASK_FULFILLED
    assert board.asks(user_id="u", status=ASK_OPEN) == []
