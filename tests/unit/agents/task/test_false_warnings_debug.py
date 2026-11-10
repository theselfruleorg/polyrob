"""Two lines warned about a state that is correct, and hid the real warnings: H-MEM
off in a sub-agent (372 lines) and an install with no balance manager (138 lines)."""
from pathlib import Path

from agents.task.agent.messages import retrieval
from modules.memory.task.null_context_manager import NullTaskContextManager


def test_the_hmem_warning_stays_for_a_real_gap_only():
    src = Path(retrieval.__file__).read_text(encoding="utf-8")
    head = src[src.index("if self.task_context_manager is None:"):]
    assert head.index("logger.warning") < head.index("elif not self.task_context_manager:")
    assert "self.logger.debug(\"H-MEM off: sub-agent isolation\")" in head
    assert not NullTaskContextManager()


def test_no_balance_manager_is_not_a_warning():
    from agents.task.task_agent_lifecycle import __file__ as path
    src = Path(path).read_text(encoding="utf-8")
    assert 'logger.debug("Balance manager not available' in src
    assert 'logger.warning("Balance manager not available' not in src
