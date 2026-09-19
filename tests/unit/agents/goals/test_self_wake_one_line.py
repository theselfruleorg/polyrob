"""056 WS7 — a chat-created goal reports back in ONE line unless asked for the report.

Prod 2026-09-19 03:00Z: the owner's chat session spent a 5-step turn "verifying
round 14" because the goal it had created re-entered it with the full result.
The 2026-08-28 rule already stopped planner/stream echoes; chat-seeded goals
still echoed. Now the wake carries `goal <id> done — <first line>` (≤ 280 chars)
unless `payload.report_back` is true.
"""
from types import SimpleNamespace


def _goal(**payload):
    return SimpleNamespace(id="abcdef123456", title="Round 14 — X outreach", user_id="rob",
                           payload={"origin_session_id": "origin-1", **payload})


def test_default_wake_is_one_line():
    from agents.task.goals.dispatcher import GoalDispatcher
    final = "ROUND 14 COMPLETE.\n\nDELIVERABLES\n- 7 touches logged\n- ..." + "x" * 900
    text = GoalDispatcher._wake_text(_goal(), final, "verified")
    assert text.startswith("goal abcdef12 done")
    assert "ROUND 14 COMPLETE." in text and "DELIVERABLES" not in text
    assert len(text) <= 280


def test_report_back_keeps_the_full_completion_text():
    from agents.task.goals.dispatcher import GoalDispatcher
    final = "ROUND 14 COMPLETE.\n\nDELIVERABLES\n- 7 touches logged"
    text = GoalDispatcher._wake_text(_goal(report_back=True), final, "verified")
    assert "DELIVERABLES" in text and "7 touches" in text


def test_unverified_wake_says_so():
    from agents.task.goals.dispatcher import GoalDispatcher
    text = GoalDispatcher._wake_text(_goal(), "did things", "unverified")
    assert "unverified" in text and text.startswith("goal abcdef12")


def test_goal_create_exposes_report_back_and_max_steps():
    from tools.goal_tools import GoalCreateAction
    a = GoalCreateAction(title="test goal", body="b", report_back=True, max_steps=30)
    assert a.report_back is True and a.max_steps == 30
    b = GoalCreateAction(title="test goal", body="b")
    assert b.report_back is False and b.max_steps is None
