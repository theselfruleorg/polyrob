"""Regression (P1 finalization): a hard wall-clock timeout (cron/goal budget) raises
asyncio.CancelledError — a BaseException that the method's `except Exception` never
catches. Without a dedicated handler, final_status stayed None and the finally
defaulted it to 'completed', mislabeling a forcibly-timed-out run as a clean success.
_run_session_impl must handle CancelledError and set a non-completed status.
"""
import ast
import inspect

from agents.task_agent_lite import TaskAgent


def _method(name):
    tree = ast.parse(inspect.getsource(TaskAgent))
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(name)


def test_run_session_impl_handles_cancelled_error():
    m = _method("_run_session_impl")
    # Find an except handler for asyncio.CancelledError / CancelledError.
    cancel_handlers = []
    for node in ast.walk(m):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            t = node.type
            name = (t.attr if isinstance(t, ast.Attribute)
                    else t.id if isinstance(t, ast.Name) else "")
            if name == "CancelledError":
                cancel_handlers.append(node)
    assert cancel_handlers, "must have an except asyncio.CancelledError handler"

    # The handler must set final_status to a non-'completed' value, and re-raise.
    handler = cancel_handlers[0]
    final_status_values = []
    for node in ast.walk(handler):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "final_status":
                    if isinstance(node.value, ast.Constant):
                        final_status_values.append(node.value.value)
    assert final_status_values, "handler must set final_status"
    assert "completed" not in final_status_values, "must NOT mark a cancelled run completed"
    assert any(isinstance(n, ast.Raise) for n in ast.walk(handler)), "must re-raise to honor cancel"
