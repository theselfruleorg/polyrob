import asyncio

from agents.task.goals.autonomy_marker import is_autonomous, mark_autonomous
from agents.task.runtime.run_as_session import run_task_as_session


def test_mark_and_check():
    mark_autonomous("sess-a")
    assert is_autonomous("sess-a") is True
    assert is_autonomous("sess-b") is False
    assert is_autonomous(None) is False
    assert is_autonomous("") is False


def test_lru_cap_keeps_recent():
    for i in range(600):
        mark_autonomous(f"cap-{i}")
    assert is_autonomous("cap-599") is True
    assert is_autonomous("cap-0") is False


class _FakeAgent:
    async def create_session(self, user_id, request):
        return {"id": "auto-sess-1"}

    async def run_session(self, user_id, session_id):
        return "Session completed successfully"


def test_run_task_as_session_marks_when_autonomous():
    sid, final = asyncio.run(run_task_as_session(
        _FakeAgent(), user_id="rob", request={"task": "x"}, autonomous=True))
    assert sid == "auto-sess-1"
    assert is_autonomous("auto-sess-1") is True


def test_run_task_as_session_default_not_marked():
    class A(_FakeAgent):
        async def create_session(self, user_id, request):
            return {"id": "manual-sess-1"}
    asyncio.run(run_task_as_session(A(), user_id="rob", request={"task": "x"}))
    assert is_autonomous("manual-sess-1") is False
