"""Regression (P1 finalization): complete_step is called ~12x/step from the async
step machinery, so it must not block the event loop. The old persistence path
retried a failed save with blocking time.sleep (up to 0.7s) while holding the lock.
It must now be a single best-effort save with no sleep, and a save failure must not
raise or stall.
"""
import inspect
import time

from agents.task.agent.tool_call_tracker import ToolCallTracker


def test_complete_step_source_has_no_blocking_sleep():
    src = inspect.getsource(ToolCallTracker.complete_step)
    # No time.sleep call in the body (a comment referencing it is fine).
    code_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    assert not any("time.sleep(" in l for l in code_lines), (
        "complete_step must not block the event loop with time.sleep"
    )


def test_complete_step_failopen_and_fast_on_save_error(monkeypatch, tmp_path):
    t = ToolCallTracker(session_id="s1")

    # Force save_to_file to fail; complete_step must swallow it and return fast.
    monkeypatch.setattr(t, "save_to_file", lambda *a, **k: False)
    start = time.monotonic()
    t.complete_step()  # must not raise, must not sleep-retry
    assert time.monotonic() - start < 0.5, "complete_step must not block on a save failure"
