"""HIGH-3: the synchronous H-MEM write (which may block on aux-LLM reflection) must run
off the event loop thread so concurrent sessions aren't frozen."""
import asyncio
import logging
import threading
import types

from agents.task.agent.core.memory_writer import MemoryWriterMixin


class _TCM:
    def __init__(self):
        self.write_thread = None

    def add_step_memory(self, **kwargs):
        # Record which thread executed the (potentially 30s-blocking) write.
        self.write_thread = threading.current_thread()
        return True

    def get_session(self, _sid):
        return None

    def save_session(self, _sid, _uid):
        pass


class _Host(MemoryWriterMixin):
    def __init__(self, tcm):
        self.task_context_manager = tcm
        self.session_id = "s1"
        self.user_id = "u1"
        self.task = "demo task"
        self.logger = logging.getLogger("test_reflection_offload")
        self.state = types.SimpleNamespace(track_finding=lambda: None)

    def _build_action_summary(self, _actions):
        return "did one thing"

    def _extract_finding_from_results(self, _results):
        return None


def test_memory_write_runs_off_event_loop_thread():
    tcm = _TCM()
    host = _Host(tcm)
    asyncio.run(host._save_step_to_memory(
        step_number=1,
        brain_state={"memory": "explored the target and learned the layout"},
        actions=[],
        results=[],
    ))
    assert tcm.write_thread is not None, "add_step_memory was never called"
    assert tcm.write_thread is not threading.main_thread(), \
        "H-MEM write ran on the main/event-loop thread (HIGH-3 regression)"
