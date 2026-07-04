"""Wiring tripwires: these fail if H-MEM stops being the primary injected/written memory."""
import inspect
from agents.task.agent.messages import retrieval
from modules.llm.messages import MessageOrigin


def test_w_inject_single_memory_block():
    """retrieval.py must inject H-MEM context as exactly one MessageOrigin.MEMORY foundation block."""
    src = inspect.getsource(retrieval)
    assert "get_context_injection(self.session_id)" in src, "H-MEM injection call removed"
    assert "MessageOrigin.MEMORY" in src, "H-MEM block no longer tagged MessageOrigin.MEMORY"
    assert src.count("MessageOrigin.MEMORY)") == 1, (
        "more than one MessageOrigin.MEMORY foundation block in retrieval.py — "
        "cross-session recall must use a DISTINCT origin (see Task 8)"
    )


def test_w_write_persists_findings():
    """memory_writer._save_step_to_memory must write H-MEM via add_step_memory every step."""
    from agents.task.agent.core import memory_writer
    src = inspect.getsource(memory_writer)
    assert "add_step_memory" in src, "H-MEM write (add_step_memory) removed from _save_step_to_memory"
    assert "brain_state" in src, "brain_state no longer threaded into the H-MEM write"


def test_w_subagent_writes_nothing_to_parent():
    """Sub-agents must wire NullTaskContextManager so they never write the parent's H-MEM."""
    from agents.task.agent.core import construction
    src = inspect.getsource(construction)
    assert "NullTaskContextManager()" in src, "sub-agent H-MEM isolation (NullTaskContextManager) removed"
