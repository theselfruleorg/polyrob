"""W1.5 — a fenced brain-memory finding never reaches the per-session H-MEM store."""
import asyncio
import logging

from agents.task.agent.core.memory_writer import MemoryWriterMixin

_FENCE = ('<owner-thread kind="tail">\nRecent lines of your ONE conversation\n'
          "owner: hello\n</owner-thread>")


class _TCM:
    def __init__(self):
        self.calls = []

    def add_step_memory(self, **kw):
        self.calls.append(kw)
        return True

    def drain_promoted_findings(self, session_id):
        return []


class _State:
    def track_finding(self):
        pass


class _Host(MemoryWriterMixin):
    def __init__(self):
        self.task_context_manager = _TCM()
        self.session_id = "s1"
        self.user_id = "u1"
        self.logger = logging.getLogger("w15")
        self.task = ""
        self.state = _State()

    def _extract_finding_from_results(self, results):
        return None


def _save(host, memory):
    asyncio.run(host._save_step_to_memory(
        step_number=1, brain_state={"memory": memory, "phase": "discovery"},
        actions=[], results=[]))


def test_fenced_finding_skips_hmem_write():
    h = _Host()
    _save(h, _FENCE)
    assert h.task_context_manager.calls == []


def test_mixed_finding_is_stored_without_the_fence():
    h = _Host()
    _save(h, "Learned the API needs a bearer token.\n" + _FENCE)
    assert len(h.task_context_manager.calls) == 1
    finding = h.task_context_manager.calls[0]["finding"]
    assert finding == "Learned the API needs a bearer token."
