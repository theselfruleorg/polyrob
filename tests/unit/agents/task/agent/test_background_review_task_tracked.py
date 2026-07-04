"""M5: _maybe_spawn_background_review did asyncio.create_task(...) and discarded the
handle. CPython may GC a task with no strong reference before it completes, and it was
never registered for cancellation at session teardown. Track it on
orchestrator._execution_tasks (like _stall_check_task) and self-remove on completion.
"""
import asyncio

from agents.task.agent.core.background_review import BackgroundReviewMixin


class _Orch:
    session_id = "s1"

    def __init__(self):
        self._execution_tasks = []


class _Stub(BackgroundReviewMixin):
    def __init__(self):
        self.orchestrator = _Orch()
        self.ran = False

    def _bg_review_should_fire(self, turn_was_productive):
        return True

    async def _run_background_review(self):
        self.ran = True
        await asyncio.sleep(0.01)


def test_background_review_task_is_tracked_and_self_removes():
    async def _run():
        s = _Stub()
        s._maybe_spawn_background_review(turn_was_productive=True)
        # Tracked immediately so it can't be GC'd / is cancellable at teardown.
        assert len(s.orchestrator._execution_tasks) == 1
        # Let it finish and self-remove.
        await asyncio.sleep(0.05)
        assert s.orchestrator._execution_tasks == []
        assert s.ran is True

    asyncio.run(_run())
