"""done() turns back ONCE per session while the session's todo list has open items.

Before 2026-10-08 the check called a TaskTool method that does not exist, so it
never ran (harness review G0).
"""
import logging
from types import SimpleNamespace

from tools.controller.action_registration import ActionRegistrationMixin


class _Tasks:
    def __init__(self, items, boom=False):
        self.items, self.boom = items, boom

    def _get_session_id(self, ctx):
        return ctx.session_id

    def _get_user_id(self, ctx):
        return None

    def get_all_tasks(self, sid, uid):
        if self.boom:
            raise OSError("unreadable")
        return self.items


class _C(ActionRegistrationMixin):
    def __init__(self, tool):
        self.logger = logging.getLogger("todo-nudge-test")
        self._tool = tool

    def get_tool(self, name):
        return self._tool if name == "task" else None


def _ctx(sid="s1"):
    return SimpleNamespace(session_id=sid, is_sub_agent=False)


def test_open_todos_nudge_once_then_let_done_through():
    c = _C(_Tasks([{"id": 1, "text": "write tests", "completed": False},
                   {"id": 2, "text": "ship", "completed": True}]))
    first = c._open_todo_nudge(_ctx())
    assert first and "1 todo(s) are still open" in first and "[1] write tests" in first
    assert "ship" not in first
    assert c._open_todo_nudge(_ctx()) is None  # bounded: the second done ends the task
    assert c._open_todo_nudge(_ctx("s2"))  # per session


def test_no_nudge_when_all_done_or_empty():
    assert _C(_Tasks([{"id": 1, "text": "x", "completed": True}]))._open_todo_nudge(_ctx()) is None
    assert _C(_Tasks([]))._open_todo_nudge(_ctx()) is None


def test_fail_open_without_tool_or_on_error():
    assert _C(None)._open_todo_nudge(_ctx()) is None
    assert _C(_Tasks([], boom=True))._open_todo_nudge(_ctx()) is None


def test_long_list_is_capped():
    items = [{"id": i, "text": f"t{i}", "completed": False} for i in range(12)]
    text = _C(_Tasks(items))._open_todo_nudge(_ctx())
    assert "12 todo(s)" in text and "and 4 more" in text and "t9" not in text


def test_no_nudge_on_the_runs_last_step():
    # A turn-back on the last step leaves no step to answer it: the run would end
    # without done and a goal would read as unfinished.
    c = _C(_Tasks([{"id": 1, "text": "write tests", "completed": False}]))
    last = SimpleNamespace(session_id="s1", is_sub_agent=False, metadata={"last_step": True})
    assert c._open_todo_nudge(last) is None
    assert c._open_todo_nudge(_ctx())  # an earlier step still turns back once


def test_execute_actions_stamps_last_step():
    import inspect
    from agents.task.agent.core import step_execution
    src = inspect.getsource(step_execution.StepExecutionMixin._execute_actions)
    assert 'metadata["last_step"]' in src and "step_info.max_steps - 1" in src
