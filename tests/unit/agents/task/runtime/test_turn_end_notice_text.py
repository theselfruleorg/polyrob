"""The turn-end notice text: the typed stop reason, the last report, the last
tool error in one line, and how to resume (connectivity review 2026-10-29 F2)."""
from agents.task.runtime.stop_reason import (
    CONTINUE_HINT, LAST_REPORT_LABEL, QUIET_STOPS, StopReason, turn_end_notice)


def test_the_notice_names_the_stop_the_error_and_the_resume():
    text = turn_end_notice(StopReason.LOOP, 9, 50,
                           last_error="cronjob_edit:\n  Nothing to change")
    lines = text.splitlines()
    assert lines[0] == "⚠️ My turn ended early: stopped because it repeated the same tool call (9/50)."
    assert lines[1] == "Last tool error: cronjob_edit: Nothing to change"
    assert lines[-1] == CONTINUE_HINT


def test_a_last_report_rides_in_the_notice_labelled():
    text = turn_end_notice("max_steps", 8, 8, last_report="Enrolled; wrote the file")
    assert f"{LAST_REPORT_LABEL}Enrolled; wrote the file" in text


def test_a_long_error_is_one_short_line():
    text = turn_end_notice("failures", 5, 50, last_error="x" * 900)
    err = [ln for ln in text.splitlines() if ln.startswith("Last tool error")][0]
    assert len(err) < 240


def test_the_quiet_stops():
    assert QUIET_STOPS == {"done", "awaiting_reply", "cancelled", "control",
                           "conversational_exit"}
