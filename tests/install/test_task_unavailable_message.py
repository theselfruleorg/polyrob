"""The 'Task package not available' error must carry the real ImportError and,
when the module maps to a pip extra, the install remedy (proposal 027 WP1).
The 0.10.0 wheel printed the bare sentinel with the cause swallowed."""

from agents.task_agent_lite import task_unavailable_message


def test_message_includes_reason_and_extra_remedy():
    msg = task_unavailable_message("No module named 'playwright'")
    assert "Task package not available" in msg
    assert "playwright" in msg
    assert "polyrob[browser]" in msg


def test_message_with_unknown_reason_keeps_the_cause():
    msg = task_unavailable_message("No module named 'left_pad'")
    assert "left_pad" in msg


def test_message_without_reason_is_still_the_sentinel():
    assert "Task package not available" in task_unavailable_message(None)
