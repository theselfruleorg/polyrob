"""B5/D5 — H-MEM finding selection must not freeze on the OLDEST findings.

`add_finding` stamps every finding with importance 1.0 and nothing recalculates
it below the 60-item prune trigger, so `_format_current_phase`'s importance sort
(stable) kept insertion order and the top-15 slice always showed the OLDEST 15
findings for phases of 16-59 findings. When importance carries no signal (all
values identical), effective importance must fall back to recency so the newest
findings win the slice.
"""
from __future__ import annotations

from modules.memory.task.context_retriever import ContextRetriever
from modules.memory.task.hierarchical_memory import HierarchicalMemory


def _build_memory(n_findings: int) -> HierarchicalMemory:
    mem = HierarchicalMemory(session_id="sel-test", task="test task")
    phase = mem.start_or_resume_phase("research", start_step=1)
    for i in range(n_findings):
        phase.add_finding(f"finding number {i:02d}")
    return mem


def test_flat_importance_selects_newest():
    mem = _build_memory(20)
    retriever = ContextRetriever(mem, max_findings_per_phase=15)
    out = retriever._format_current_phase("research")
    assert out is not None
    # Newest 15 (indices 5..19) shown; oldest 5 dropped.
    assert "finding number 19" in out
    assert "finding number 05" in out
    assert "finding number 04" not in out
    assert "finding number 00" not in out


def test_flat_importance_keeps_chronological_display():
    mem = _build_memory(20)
    retriever = ContextRetriever(mem, max_findings_per_phase=15)
    out = retriever._format_current_phase("research")
    assert out.index("finding number 05") < out.index("finding number 19")


def test_varied_importance_still_wins():
    """When importance DOES carry signal, it must keep deciding the slice."""
    mem = _build_memory(20)
    phase = mem.get_current_phase_memory()
    phase.finding_importance[0] = 5.0  # oldest finding explicitly important
    retriever = ContextRetriever(mem, max_findings_per_phase=15)
    out = retriever._format_current_phase("research")
    assert "finding number 00" in out


def test_under_cap_shows_all():
    mem = _build_memory(10)
    retriever = ContextRetriever(mem, max_findings_per_phase=15)
    out = retriever._format_current_phase("research")
    for i in range(10):
        assert f"finding number {i:02d}" in out
