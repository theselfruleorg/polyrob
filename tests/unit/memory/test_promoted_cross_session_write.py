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


def test_reflection_summary_drains_cross_session(monkeypatch):
    """SA-07 (2026-07-06 review): the aux-LLM reflection summary was written to
    phase_memory.summary and DISCARDED at session end — only raw findings synced
    cross-session. The synthesized summary now rides the same promoted-findings
    drain (tagged with its phase) exactly once."""
    tcm = _tcm()
    tcm.create_session("sr", task="task")
    tcm.add_step_memory("sr", 1, {"phase": "p1", "memory": "raw fact"}, "a", finding="raw fact")
    monkeypatch.setattr(
        TaskContextManager, "_llm_consolidate",
        lambda self, findings: "Synthesized: the raw facts add up to X.")
    # the phase manager keeps step-1 work in 'discovery' (premature-transition guard)
    tcm._trigger_reflection("sr", "discovery")
    drained = tcm.drain_promoted_findings("sr")
    joined = "\n".join(drained)
    assert "Synthesized: the raw facts add up to X." in joined
    assert "discovery" in joined  # phase provenance tag
    # exactly once — a second drain must not re-emit the summary
    assert "Synthesized" not in "\n".join(tcm.drain_promoted_findings("sr"))


def test_concat_fallback_summary_not_synced(monkeypatch):
    """The concatenation fallback adds no information over the raw findings that
    already sync — only a genuine LLM synthesis is worth a cross-session row."""
    tcm = _tcm()
    tcm.create_session("sc", task="task")
    tcm.add_step_memory("sc", 1, {"phase": "p1", "memory": "raw fact"}, "a", finding="raw fact")
    monkeypatch.setattr(TaskContextManager, "_llm_consolidate", lambda self, findings: None)
    tcm._trigger_reflection("sc", "discovery")
    drained = tcm.drain_promoted_findings("sc")
    assert not any("[phase-summary" in f for f in drained)
