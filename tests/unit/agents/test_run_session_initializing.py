"""Regression: run_session must treat 'initializing' as a runnable (pre-run) status.

Bug (2026-07-01): a warm STEER resume (owner voice/text message continuing a suspended
session) recreates the orchestrator, whose __init__ sets session status='initializing'.
run_session had no 'initializing' branch, so it fell through to "Cannot run session in
status: initializing" — the message was queued but never run → Rob never replied to the
owner (voice OR text). This was the true owner-chat outage (the BaseError + load_from_disk
fixes only got the flow far enough to reveal it).
"""
import pathlib

_TAL = pathlib.Path(__file__).resolve().parents[3] / "agents" / "task_agent_lite.py"


def test_run_session_treats_initializing_as_runnable():
    text = _TAL.read_text()
    # the created branch now also accepts initializing and transitions it to running
    assert "('created', 'initializing')" in text
    # it must NOT be left to dead-end only in the else "Cannot run session"
    assert "Cannot run session" in text  # the else still exists for truly-unrunnable states
