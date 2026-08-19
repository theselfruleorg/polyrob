"""A retry must be told what the previous attempt already produced.

46 goals died on "run ended without completing (no done() — likely ran out of
steps)". Each retry then started from the title again and re-did the same first
20 steps, so a goal needing 25 steps could never finish — it just burned its two
retries and gave up.

The attempts ledger already carries what FAILED. This adds what SUCCEEDED: the
artifacts the previous attempt really wrote, verified against the ledger, so the
run continues instead of restarting.
"""
from agents.task.goals.board import Goal
from agents.task.goals.context import build_goal_run_task


def test_prior_artifacts_appear_in_the_retry_prompt():
    goal = Goal(id="g1", user_id="rob", title="x402 Round 4",
                payload={"attempts": [{"error": "ran out of steps", "ts": 1}]})

    task = build_goal_run_task(
        goal, None,
        prior_artifacts=[("x402-round4-decode.md", 2845),
                         ("sweep-ledger.json", 10240)])

    assert "ALREADY PRODUCED" in task
    assert "x402-round4-decode.md" in task
    assert "sweep-ledger.json" in task


def test_the_prompt_tells_the_run_to_continue_not_restart():
    goal = Goal(id="g1", user_id="rob", title="x402 Round 4",
                payload={"attempts": [{"error": "ran out of steps", "ts": 1}]})

    task = build_goal_run_task(goal, None,
                              prior_artifacts=[("evidence.md", 100)])

    lowered = task.lower()
    assert "do not re-create" in lowered or "do not recreate" in lowered
    assert "continue" in lowered


def test_no_prior_artifacts_leaves_the_prompt_unchanged():
    """Byte-identical when the previous attempt produced nothing."""
    goal = Goal(id="g1", user_id="rob", title="x402 Round 4",
                payload={"attempts": [{"error": "ran out of steps", "ts": 1}]})

    assert build_goal_run_task(goal, None, prior_artifacts=[]) == \
        build_goal_run_task(goal, None)


def test_a_first_attempt_is_unchanged():
    goal = Goal(id="g1", user_id="rob", title="x402 Round 4")

    assert "ALREADY PRODUCED" not in build_goal_run_task(goal, None)
