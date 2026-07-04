"""Tests for drain_promoted_findings — high-water-mark drain of H-MEM curated findings."""
from core.config import BotConfig
from modules.memory.task.task_context_manager import TaskContextManager


def _tcm():
    return TaskContextManager(name="t-drain", config=BotConfig())


def test_drain_returns_curated_then_empty():
    tcm = _tcm()
    tcm.create_session("s", task="task")
    tcm.add_step_memory("s", 1, {"phase": "p1", "memory": "fact one"}, "a", finding="fact one")
    first = tcm.drain_promoted_findings("s")
    assert any("fact one" in f for f in first), f"expected curated finding, got {first}"
    # Second drain with no new findings returns nothing (high-water mark).
    assert tcm.drain_promoted_findings("s") == [], "drain must not re-emit already-drained findings"
    tcm.add_step_memory("s", 2, {"phase": "p1", "memory": "fact two"}, "a", finding="fact two")
    second = tcm.drain_promoted_findings("s")
    assert any("fact two" in f for f in second), f"expected only the new finding, got {second}"
    assert not any("fact one" in f for f in second), "must not re-emit fact one"


def test_drain_unknown_session_empty():
    assert _tcm().drain_promoted_findings("nope") == []


def test_drain_exactly_once_under_pruning():
    tcm = _tcm()
    tcm.create_session("sp", task="long task")
    for i in range(70):  # exceed MAX_FINDINGS_PER_PHASE (default 60) -> pruning engages
        tcm.add_step_memory("sp", i + 1, {"phase": "p1", "memory": f"fact {i}"}, "a", finding=f"fact {i}")
    drained = list(tcm.drain_promoted_findings("sp"))
    tcm.add_step_memory("sp", 100, {"phase": "p1", "memory": "fact LATE"}, "a", finding="fact LATE")
    second = tcm.drain_promoted_findings("sp")
    assert "fact LATE" in second, "finding added after the phase hit its cap was never drained (high-water-mark bug)"
    drained += second
    assert len(drained) == len(set(drained)), "a finding was drained more than once"


def test_null_context_manager_drains_empty():
    from modules.memory.task.null_context_manager import NullTaskContextManager
    assert NullTaskContextManager().drain_promoted_findings("s") == []
