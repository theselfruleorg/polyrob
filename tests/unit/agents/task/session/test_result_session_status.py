"""F7b (live-test): a permanent-error halt must not be reported as a completed session.

error_recovery ends the loop with a final ActionResult carrying .error + is_done=True,
so agent.run() returns a truthy history. The old `"completed" if result` logic labeled
that a success → goals/cron recorded a 402/billing halt as done. _result_session_status
classifies a terminal .error as 'error'; a real done() (error=None) stays 'completed'.
"""
from types import SimpleNamespace

from agents.task.session.execution import _result_session_status


def _result(error):
    last = SimpleNamespace(error=error, is_done=True, extracted_content="x")
    item = SimpleNamespace(result=[last])
    return SimpleNamespace(history=[item])


def test_terminal_error_halt_is_error():
    assert _result_session_status(_result("PERMANENT ERROR: 402 ... Session halted")) == "error"


def test_genuine_completion_is_completed():
    assert _result_session_status(_result(None)) == "completed"


def test_falsy_result_is_failed():
    assert _result_session_status(None) == "failed"


def test_malformed_result_defaults_completed_not_crash():
    # Defensive: a truthy result with no usable history must not raise.
    assert _result_session_status(SimpleNamespace(history=[])) == "completed"
