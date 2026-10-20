"""drain_promoted_findings hands ONLY the reflection summaries (LLM-consolidated)
to the cross-session store, each once. Raw per-step findings stay in H-MEM
(2026-10-14: they were the step narration / tool echo noise)."""
from core.config import BotConfig
from modules.memory.task.task_context_manager import TaskContextManager


def _tcm():
    return TaskContextManager(name="t-drain", config=BotConfig())


def test_raw_step_findings_never_drain_cross_session():
    tcm = _tcm()
    tcm.create_session("s", task="task")
    tcm.add_step_memory("s", 1, {"phase": "p1", "memory": "fact one"}, "a", finding="fact one")
    assert tcm.drain_promoted_findings("s") == []


def test_drain_unknown_session_empty():
    assert _tcm().drain_promoted_findings("nope") == []


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
        lambda self, findings, session_id="": "Synthesized: the raw facts add up to X.")
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


def test_unsent_summaries_are_given_back_to_a_live_session():
    tcm = _tcm()
    tcm.create_session("sr5", task="task", user_id="u1")
    summary = "[phase-summary:p1] Bridge confirmed."
    assert tcm.release_unsent_findings("sr5", [summary], "u1")
    assert tcm.drain_promoted_findings("sr5") == [summary]
    assert tcm.drain_promoted_findings("sr5") == []


def test_a_closed_session_cannot_take_back_summaries():
    assert _tcm().release_unsent_findings("gone", ["[phase-summary:p1] x"], "u1") is False


def test_requeued_summaries_stay_within_one_stored_row(monkeypatch):
    """A store that stays full must not grow the queue past what one row holds:
    the next drain is joined into ONE row cut at MEMORY_ROW_MAX_CHARS, so the
    oldest summaries are dropped (logged) instead of silently cut off the end."""
    monkeypatch.setenv("MEMORY_ROW_MAX_CHARS", "4000")
    tcm = _tcm()
    tcm.create_session("sr6", task="task", user_id="u1")
    batch = [f"[phase-summary:p{i}] " + "x" * 900 for i in range(10)]  # oldest first
    assert tcm.release_unsent_findings("sr6", batch, "u1")
    drained = tcm.drain_promoted_findings("sr6")
    assert len("\n".join(drained)) <= 4000
    assert drained and drained[-1] == batch[-1]  # the newest is kept
    assert batch[0] not in drained
