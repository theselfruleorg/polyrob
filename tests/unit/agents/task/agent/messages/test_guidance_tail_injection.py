"""P1 (2026-07-02 architecture fix): new user messages must land at the history TAIL.

Bug (prod session fa1212de, 102 msgs): ``inject_user_guidance`` inserted the
"NEW USER MESSAGE - PRIORITY INPUT" frame at history position 1. On a resumed
long-lived owner chat every real owner message ("Show me the results of the
research", "yo", ...) piled up at the TOP of the history while the tail filled
with stale "✅ Task Complete — No new user input" turns. The model, reading the
most recent context, kept concluding there was no new input and never answered
the owner. A new user turn must be appended as the LATEST message so the model
sees it as the current input, and stamped with origin + wall-clock time so
accumulated frames are distinguishable.
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import AIMessage, HumanMessage, MessageOrigin


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Original task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=8000,
        session_id="s-guidance",
    )


def _polluted_mm(n_turns=20):
    """Simulate a long resumed owner-chat history ending in no-op done turns."""
    mm = _mm()
    for i in range(n_turns):
        mm._add_message_with_tokens(
            HumanMessage(content=f"[MEMORY FROM PREVIOUS STEP] step {i}"))
        mm._add_message_with_tokens(
            AIMessage(content="✅ Task Complete. No new user input."), _internal=True)
    return mm


def test_new_user_message_is_last_history_message():
    mm = _polluted_mm()
    mm.inject_user_guidance(
        [{"text": "Show me the results of the research", "kind": "comment", "metadata": {}}],
        session_context={"continuation": True},
    )
    last = mm.history.messages[-1].message
    assert isinstance(last, HumanMessage)
    content = last.content if isinstance(last.content, str) else str(last.content)
    assert "Show me the results of the research" in content


def test_new_user_message_not_buried_at_top():
    mm = _polluted_mm()
    mm.inject_user_guidance(
        [{"text": "FRESH OWNER QUESTION", "kind": "comment", "metadata": {}}],
        session_context={"continuation": True},
    )
    top_texts = [
        (m.message.content if isinstance(m.message.content, str) else "")
        for m in list(mm.history.messages)[:3]
    ]
    assert not any("FRESH OWNER QUESTION" in t for t in top_texts)


def test_injected_frame_carries_timestamp():
    mm = _mm()
    mm.inject_user_guidance(
        [{"text": "hello", "kind": "comment", "metadata": {}}],
        session_context={"continuation": True},
    )
    last = mm.history.messages[-1].message
    content = last.content if isinstance(last.content, str) else str(last.content)
    # Frame must carry an absolute date so stale frames can't masquerade as new
    import re
    assert re.search(r"\d{4}-\d{2}-\d{2}", content), content[:200]


def test_genuine_user_kind_gets_user_origin():
    mm = _mm()
    mm.inject_user_guidance(
        [{"text": "hi", "kind": "comment", "metadata": {}}],
        session_context={"continuation": True},
    )
    assert mm.history.messages[-1].message.origin == MessageOrigin.USER


def test_forged_self_wake_kind_gets_self_wake_origin():
    mm = _mm()
    mm.inject_user_guidance(
        [{"text": "continue autonomously", "kind": "self_wake", "metadata": {}}],
        session_context={"continuation": True},
    )
    assert mm.history.messages[-1].message.origin == MessageOrigin.SELF_WAKE


def test_mixed_kinds_keep_user_origin():
    # A real user message must never be demoted by a co-drained forged turn.
    mm = _mm()
    mm.inject_user_guidance(
        [
            {"text": "wake", "kind": "self_wake", "metadata": {}},
            {"text": "real question", "kind": "comment", "metadata": {}},
        ],
        session_context={"continuation": True},
    )
    assert mm.history.messages[-1].message.origin == MessageOrigin.USER
