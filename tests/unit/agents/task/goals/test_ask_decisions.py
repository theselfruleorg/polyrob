"""Task 9 / G-2 — GoalBoard ask-decision primitives: `decide_ask` (generalizes
`fulfill_ask` with a real reject outcome), `create_ask`'s new `extra_payload`/
`force` kwargs, and `consume_ask_grant`'s atomic one-shot claim.

These are the durable store `tools/controller/approval_queue.py::OwnerQueueApprover`
polls/writes — covered here in isolation from the async provider (see
`tests/unit/tools/controller/test_approval_queue.py` for the end-to-end flow).
"""
import pytest

from agents.task.goals.board import (
    ASK_FULFILLED, ASK_OPEN, ASK_REJECTED, GoalBoard, STATUS_BLOCKED, STATUS_READY,
)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


def test_decide_ask_approve_is_equivalent_to_fulfill(board):
    g = board.create(user_id="rob", title="Post the launch thread")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="no access")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="no access")  # trips breaker -> blocked
    a = board.create_ask(user_id="rob", what="Grant access", blocks_goal_ids=[g.id])

    ok, unblocked = board.decide_ask(a.id, user_id="rob", approved=True)

    assert ok is True and unblocked == 1
    row = board.get(a.id)
    assert row.status == ASK_FULFILLED
    assert row.payload["decision"] == "approved"
    assert board.get(g.id).status == STATUS_READY


def test_decide_ask_reject_never_unblocks(board):
    g = board.create(user_id="rob", title="Post the launch thread")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="no access")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="no access")
    a = board.create_ask(user_id="rob", what="Grant access", blocks_goal_ids=[g.id])

    ok, unblocked = board.decide_ask(a.id, user_id="rob", approved=False)

    assert ok is True and unblocked == 0
    row = board.get(a.id)
    assert row.status == ASK_REJECTED
    assert row.payload["decision"] == "rejected"
    assert board.get(g.id).status == STATUS_BLOCKED  # untouched by a rejection


def test_decide_ask_wrong_tenant_noop(board):
    a = board.create_ask(user_id="rob", what="Grant access")
    ok, unblocked = board.decide_ask(a.id, user_id="mallory", approved=True)
    assert ok is False and unblocked == 0
    assert board.get(a.id).status == ASK_OPEN


def test_decide_ask_already_decided_is_a_noop(board):
    a = board.create_ask(user_id="rob", what="Grant access")
    ok1, _ = board.decide_ask(a.id, user_id="rob", approved=True)
    assert ok1 is True
    ok2, _ = board.decide_ask(a.id, user_id="rob", approved=False)  # can't flip a decided ask
    assert ok2 is False
    assert board.get(a.id).status == ASK_FULFILLED  # the FIRST decision wins


def test_fulfill_ask_still_works_unchanged(board):
    a = board.create_ask(user_id="rob", what="Grant access")
    ok, unblocked = board.fulfill_ask(a.id, user_id="rob")
    assert ok is True and unblocked == 0
    assert board.get(a.id).status == ASK_FULFILLED


# --- create_ask: extra_payload + force ----------------------------------------------

def test_create_ask_extra_payload_is_stored(board):
    a = board.create_ask(user_id="rob", what="Approve x402_request? [abc123]",
                         extra_payload={"ask_kind": "tool_approval", "request_hash": "abc123"},
                         force=True)
    row = board.get(a.id)
    assert row.payload["ask_kind"] == "tool_approval"
    assert row.payload["request_hash"] == "abc123"
    assert row.payload["blocks_goal_ids"] == []


def test_create_ask_force_skips_fuzzy_dedup(board, monkeypatch):
    """Two DISTINCT tool-approval requests can carry a near-identical generic
    title ("Approve x402_request?") — force=True must NOT fuzzy-merge them into
    one ask (an exact-hash caller already did its own precise dedup)."""
    monkeypatch.setenv("GOAL_DEDUP_THRESHOLD", "0.3")  # aggressive, to prove force wins
    a1 = board.create_ask(user_id="rob", what="Approve x402_request? [aaa111]",
                          extra_payload={"request_hash": "aaa111"}, force=True)
    a2 = board.create_ask(user_id="rob", what="Approve x402_request? [bbb222]",
                          extra_payload={"request_hash": "bbb222"}, force=True)
    assert a1.id != a2.id
    assert len(board.asks(user_id="rob", status=ASK_OPEN)) == 2


def test_create_ask_without_force_still_dedups_by_default(board):
    """Backward compatibility: existing callers (budget gate, blocker escalation)
    never pass force — the original fuzzy dedup behavior is untouched."""
    a1 = board.create_ask(user_id="rob", what="Grant Twitter write access")
    a2 = board.create_ask(user_id="rob", what="Grant twitter WRITE access!")
    assert a2.id == a1.id


# --- consume_ask_grant: atomic one-shot claim ----------------------------------------

def test_consume_ask_grant_wins_once(board):
    a = board.create_ask(user_id="rob", what="Approve x402_request? [xyz]",
                         extra_payload={"grant_consumed": False}, force=True)
    assert board.consume_ask_grant(a.id) is True
    assert board.consume_ask_grant(a.id) is False  # already consumed
    assert board.get(a.id).payload["grant_consumed"] is True


def test_consume_ask_grant_no_flag_present_is_a_noop(board):
    a = board.create_ask(user_id="rob", what="Grant access")  # no grant_consumed key at all
    assert board.consume_ask_grant(a.id) is False


def test_consume_ask_grant_unknown_id_is_a_noop(board):
    assert board.consume_ask_grant("nope") is False


def test_consume_ask_grant_survives_payload_compaction(board):
    """Task 9b: the same bug class Task 9 fixed in
    ``modules/x402/invoicing.py`` — a string ``REPLACE``/``LIKE`` match on a
    spaced ``json.dumps`` literal silently stops matching once ANY
    ``json_set`` touches the SAME blob (SQLite's ``json_set`` re-serializes
    the WHOLE blob COMPACTLY). No ``json_set`` currently touches
    ``goals.payload`` anywhere in this codebase — confirmed by a repo-wide
    grep — so this was LATENT, not reachable today. This test simulates a
    hypothetical future dedup/backfill routine (mirroring
    ``modules.database.x402_tables.dedupe_and_create_subscription_pending_unique_index``,
    the REAL routine that triggers the same class of bug on
    ``x402_payment_requests.metadata``) touching this row with ``json_set``,
    to prove ``consume_ask_grant``'s fix is robust to it landing later."""
    from core.sqlite_util import execute_retry

    a = board.create_ask(user_id="rob", what="Approve x402_request? [xyz]",
                         extra_payload={"grant_consumed": False}, force=True)

    # Recompact the payload blob the way a future json_set-based dedup would.
    execute_retry(
        board.db_path,
        "UPDATE goals SET payload = json_set(payload, '$.dedup_touched', 1) WHERE id=?",
        (a.id,),
    )
    # Prove the recompaction actually happened: the OLD spaced-literal
    # REPLACE/LIKE predicate this test guards against no longer matches this
    # row at all — without this assertion the test would not demonstrate the
    # bug's precondition, only the fix's postcondition.
    still_spaced = execute_retry(
        board.db_path,
        "SELECT id FROM goals WHERE id=? AND payload LIKE '%\"grant_consumed\": false%'",
        (a.id,), fetch="all",
    )
    assert list(still_spaced or []) == [], (
        "setup did not actually recompact the payload blob — this test "
        "would not be exercising the Task 9b bug"
    )

    assert board.consume_ask_grant(a.id) is True
    assert board.consume_ask_grant(a.id) is False  # already consumed
    assert board.get(a.id).payload["grant_consumed"] is True


def test_decide_ask_answer_rides_into_the_unblocked_goal(board):
    """A27 (2026-09-21): the owner's ANSWER lands on the ask and on each
    dependent goal's owner_unblocked stamp, and the retry prompt renders it —
    an unblocked run learns WHAT the owner said, not only that it was unblocked."""
    from agents.task.goals.context import build_goal_run_task
    g = board.create(user_id="rob", title="Post the launch thread")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="which key?")
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_failure(g.id, error="which key?")
    a = board.create_ask(user_id="rob", what="Which API key?", blocks_goal_ids=[g.id])

    ok, unblocked = board.decide_ask(a.id, user_id="rob", approved=True,
                                     answer="  use the SANDBOX key \n please ")

    assert ok is True and unblocked == 1
    assert board.get(a.id).payload["answer"] == "use the SANDBOX key \n please".strip()
    dep = board.get(g.id)
    assert dep.payload["owner_unblocked"]["answer"].startswith("use the SANDBOX key")
    prompt = build_goal_run_task(dep, None)
    assert "the owner answered: use the SANDBOX key" in prompt


def test_decide_ask_empty_answer_writes_nothing(board):
    a = board.create_ask(user_id="rob", what="Grant access")
    ok, _ = board.decide_ask(a.id, user_id="rob", approved=False, answer="   ")
    assert ok is True
    assert "answer" not in board.get(a.id).payload


def test_decide_ask_refused_decision_records_no_answer(board):
    a = board.create_ask(user_id="rob", what="Grant access")
    ok, _ = board.decide_ask(a.id, user_id="someone-else", approved=True, answer="x")
    assert ok is False
    assert "answer" not in (board.get(a.id).payload or {})
