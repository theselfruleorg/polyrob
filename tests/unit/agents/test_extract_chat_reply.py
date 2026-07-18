"""_extract_chat_reply — the reply extractor behind chat_once, the telegram
surface and (as display fallback) RunOutcome.

Live root cause (goal 58a1385d18bf): priority-1 read ``agent.state.history``,
but AgentState has NO ``history`` field — the ledger lives on ``agent.history``
— so priority-1 ALWAYS AttributeError'd silently and priority-2 returned the
P2-16 placeholder AIMessage "Processing actions" (added to history AFTER the
clean "✅ Task Complete" message, so it wins the reverse scan)."""
from types import SimpleNamespace

from agents.task_agent_lite import TaskAgent
from modules.llm.messages import AIMessage


def _task_agent_with(orch):
    ta = TaskAgent.__new__(TaskAgent)
    ta._registry = SimpleNamespace(get=lambda sid: orch)
    return ta


def _mm(messages):
    managed = [SimpleNamespace(message=m) for m in messages]
    return SimpleNamespace(history=SimpleNamespace(messages=managed))


def test_priority1_reads_agent_history_not_state_history():
    """The done() text must come from agent.history (the real ledger attr) —
    AgentState has no history field, so state.history can never work."""
    hist = SimpleNamespace(
        is_done=lambda: True,
        final_result=lambda: "OUTCOME: BLOCKED — x402 payment request store unavailable",
    )
    agent = SimpleNamespace(
        history=hist,
        state=SimpleNamespace(n_steps=3),  # deliberately NO .history
        message_manager=None,
    )
    orch = SimpleNamespace(agents={"main": agent})
    ta = _task_agent_with(orch)
    assert "x402" in ta._extract_chat_reply("s1")


def test_priority2_skips_framework_placeholder_messages():
    """The placeholder AIMessage ('Processing actions') is framework-authored
    and must never be returned as the agent's reply; the clean completion
    message behind it wins."""
    agent = SimpleNamespace(
        history=SimpleNamespace(is_done=lambda: False, final_result=lambda: None),
        state=SimpleNamespace(n_steps=3),
        message_manager=_mm([
            AIMessage(content="✅ Task Complete\n\nWrote the report to workspace/report.md"),
            AIMessage(content="Processing actions"),
        ]),
    )
    orch = SimpleNamespace(agents={"main": agent})
    ta = _task_agent_with(orch)
    assert ta._extract_chat_reply("s1") == "Wrote the report to workspace/report.md"


def test_placeholder_never_returned_even_as_last_resort():
    agent = SimpleNamespace(
        history=SimpleNamespace(is_done=lambda: False, final_result=lambda: None),
        state=SimpleNamespace(n_steps=1),
        message_manager=_mm([AIMessage(content="Processing actions")]),
    )
    orch = SimpleNamespace(agents={"main": agent})
    ta = _task_agent_with(orch)
    assert ta._extract_chat_reply("s1") == ""
