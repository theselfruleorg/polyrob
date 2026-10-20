"""A goal runs once: a create whose text describes a cadence is told so.

2026-10-15: the X growth "durable runner" (goal 3da94eddc46f) said "read it every
run" and "roughly one post per day", passed its acceptance on the first run, went
`done`, and nothing ran it again. The owner asked why it stopped.
"""
from tools.goal_tools import _recurring_work_warning

RUNNER_BODY = ("Durable runner for the reprogrammed X growth programme (read it every run). "
               "At most one programme post per run. Roughly one post per day.")


def test_the_x_runner_text_is_flagged():
    w = _recurring_work_warning("X GROWTH PROGRAMME 2 — RUNNER", RUNNER_BODY, "")
    assert w is not None
    assert "cronjob_schedule" in w and "runs ONCE" in w


def test_daily_and_every_hours_are_flagged():
    assert _recurring_work_warning("Daily digest of mentions", "", "")
    assert _recurring_work_warning("Check prices", "every 4 hours compare the book", "")
    assert _recurring_work_warning("t", "", "Each run appends one ledger line")


def test_a_one_shot_goal_is_not_flagged():
    assert _recurring_work_warning("Deploy the landing page",
                                   "Build it, publish it, verify the URL returns 200.",
                                   "http_ok on the URL") is None
    assert _recurring_work_warning("Bridge 2 ETH to Ethereum",
                                   "Quote, bridge, confirm arrival within the day.", "") is None
