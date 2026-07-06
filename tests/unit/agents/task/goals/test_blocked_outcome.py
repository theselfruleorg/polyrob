"""§3.1 — honest BLOCKED exit: prompt clause + outcome parsing + board block.

The prod agent had no vocabulary to honestly fail a goal (its only exits were
'done' or silence). These tests pin the new affordance: the goal-run prompt
teaches `OUTCOME: BLOCKED — <need>`, `parse_blocked_outcome` recognises it
robustly, and the board can flip a below-breaker 'ready' row straight to
'blocked' when the agent itself declared retrying won't help.
"""
from agents.task.goals.board import GoalBoard, STATUS_BLOCKED, STATUS_READY
from agents.task.goals.context import (
    build_goal_run_task,
    extract_outcome_line,
    parse_blocked_outcome,
)


def test_goal_run_prompt_teaches_blocked_exit(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="Post the announcement")
    t = build_goal_run_task(g, None)
    assert "OUTCOME: BLOCKED" in t
    assert "do NOT report success" in t


def test_parse_blocked_outcome_variants():
    # em-dash, hyphen, colon, no separator, case-insensitive
    assert parse_blocked_outcome("BLOCKED — need TWITTER_ENABLED") == "need TWITTER_ENABLED"
    assert parse_blocked_outcome("BLOCKED - need creds") == "need creds"
    assert parse_blocked_outcome("blocked: owner approval") == "owner approval"
    assert parse_blocked_outcome("Blocked need X access") == "need X access"
    assert parse_blocked_outcome("BLOCKED") == ""


def test_parse_blocked_outcome_rejects_non_blocked():
    assert parse_blocked_outcome("posted tweet 123") is None
    assert parse_blocked_outcome("NONE — nothing to do") is None
    assert parse_blocked_outcome(None) is None
    # 'blocked' mentioned mid-sentence is NOT a declaration
    assert parse_blocked_outcome("wrote file; twitter was blocked earlier") is None


def test_extract_then_parse_end_to_end():
    final = "I drafted it but cannot post.\nOUTCOME: BLOCKED — Twitter write is disabled"
    assert parse_blocked_outcome(extract_outcome_line(final)) == "Twitter write is disabled"


def test_block_from_ready_flips_only_ready_rows(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="Post it")
    b.claim(g.id, "w", ttl_seconds=60)
    # below-breaker failure returns the goal to ready
    b.record_failure(g.id, error="agent declared BLOCKED: need TWITTER_ENABLED")
    assert b.get(g.id).status == STATUS_READY
    assert b.block_from_ready(g.id, error="agent declared BLOCKED: need TWITTER_ENABLED") is True
    assert b.get(g.id).status == STATUS_BLOCKED
    assert "gave_up" in [e["kind"] for e in b.events(g.id)]


def test_block_from_ready_respects_owner_intervention(tmp_path):
    """An owner-cancelled row must never be resurrected into 'blocked'."""
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="Post it")
    b.claim(g.id, "w", ttl_seconds=60)
    b.record_failure(g.id, error="x")  # back to ready
    b.cancel(g.id)
    assert b.block_from_ready(g.id, error="x") is False
    assert b.get(g.id).status == "cancelled"
