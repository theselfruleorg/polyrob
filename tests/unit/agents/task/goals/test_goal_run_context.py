from agents.task.goals.board import GoalBoard
from agents.task.goals.context import build_goal_run_task, extract_outcome_line


def _mk(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    o = b.create_objective(user_id="rob", title="Get 100k followers on X",
                           body="X is distribution for substack posts.")
    g = b.create(user_id="rob", title="Draft memory-arch thread", parent_id=o.id,
                 body="Draft a 5-tweet thread.",
                 payload={"acceptance": "a draft file under project/drafts/"})
    return b, o, g


def test_task_contains_objective_body_acceptance_outcome(tmp_path):
    b, o, g = _mk(tmp_path)
    t = build_goal_run_task(g, o)
    assert "STANDING OBJECTIVE" in t and "Get 100k followers on X" in t
    assert "Draft a 5-tweet thread." in t
    assert "Definition of done" in t and "project/drafts/" in t
    assert "OUTCOME:" in t


def test_task_without_objective(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="One-off", body="Do a thing.")
    t = build_goal_run_task(g, None)
    assert "one-off goal" in t.lower() and "Do a thing." in t


def test_task_attempts_ledger_rides_into_retry_prompt(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="Post intro", body="Post it.",
                 payload={"attempts": [{"ts": 100.0, "error": "BLOCKED: no telegram capability"}]})
    t = build_goal_run_task(g, None)
    assert "PREVIOUS ATTEMPT" in t
    assert "no telegram capability" in t


def test_task_owner_unblocked_overrides_failure_memory(tmp_path):
    """2026-07-14 night-2: after `owner fulfill`, the retry must be told the
    blocker is FIXED — otherwise the agent declares BLOCKED from stale memory
    without retrying the real action."""
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="Post intro", body="Post it.",
                 payload={"attempts": [{"ts": 100.0, "error": "BLOCKED: no telegram capability"}],
                          "owner_unblocked": {"ts": 200.0, "ask_id": "a1"}})
    t = build_goal_run_task(g, None)
    assert "OWNER UNBLOCKED" in t
    assert "retry" in t.lower()
    assert "PREVIOUS ATTEMPT (this goal was retried" not in t


def test_task_stale_owner_unblocked_keeps_failure_ledger(tmp_path):
    """A NEW failure after the fulfillment means the unblock note is stale —
    show the normal ledger again."""
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="Post intro", body="Post it.",
                 payload={"attempts": [{"ts": 300.0, "error": "BLOCKED: still failing"}],
                          "owner_unblocked": {"ts": 200.0, "ask_id": "a1"}})
    t = build_goal_run_task(g, None)
    assert "PREVIOUS ATTEMPT" in t and "OWNER UNBLOCKED" not in t


def test_extract_outcome_line():
    text = "did stuff\nmore\nOUTCOME: posted tweet 123, wrote project/a.md"
    assert extract_outcome_line(text) == "posted tweet 123, wrote project/a.md"
    assert extract_outcome_line("no outcome here") is None
    assert extract_outcome_line(None) is None


def test_set_outcome_on_done_goal(tmp_path):
    b = GoalBoard(str(tmp_path / "g.db"))
    g = b.create(user_id="rob", title="t goal")
    b.claim(g.id, "w", ttl_seconds=60)
    b.record_success(g.id, result="OUTCOME: x")
    assert b.set_outcome(g.id, "x") is True
    assert b.get(g.id).payload["outcome"] == "x"


# --- QW-2 (2026-07-19, proposal 021): attach guidance in the run prompt -----

def test_goal_run_task_teaches_deliverable_attachment():
    """The agent-messaged-first case suppresses the framework completion push
    (`not run.user_messages`), so the AGENT itself must know to attach files —
    today's x402-recon flow sent `media_paths=None` while naming the file."""
    from agents.task.goals.board import Goal
    from agents.task.goals.context import build_goal_run_task
    text = build_goal_run_task(Goal(id="g", user_id="u", title="t"), None)
    assert "media_paths" in text
