"""Final-review fix (T1.1): _terminal_error_text must surface the terminal
step's error text so execute_session's success-path branch can include an
"error" key (previously dropped, forcing task_agent_lite to fall back to
"Unknown error" even on a RUN_BUDGET_USD halt).
"""
from types import SimpleNamespace

import agents.task.agent.core.run_budget as rb
from agents.task.session.execution import _terminal_error_text


def _result(error):
    last = SimpleNamespace(error=error, is_done=True, extracted_content="x")
    item = SimpleNamespace(result=[last])
    return SimpleNamespace(history=[item])


def test_extracts_run_budget_marker_text():
    msg = f"{rb.RUN_BUDGET_MARKER}: session provider spend $0.25 reached RUN_BUDGET_USD $0.10"
    assert _terminal_error_text(_result(msg)) == msg
    assert rb.RUN_BUDGET_MARKER in _terminal_error_text(_result(msg))


def test_extracts_permanent_error_text():
    msg = "PERMANENT ERROR: 402 ... Session halted"
    assert _terminal_error_text(_result(msg)) == msg


def test_none_error_returns_none():
    assert _terminal_error_text(_result(None)) is None


def test_falsy_result_returns_none():
    assert _terminal_error_text(None) is None


def test_malformed_result_returns_none_not_crash():
    assert _terminal_error_text(SimpleNamespace(history=[])) is None
