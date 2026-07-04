"""P3 (2026-07-02): forged re-entry turns must not masquerade as user messages.

A self-wake / delegation-result batch historically got the same
"🔄 NEW USER MESSAGE - PRIORITY INPUT" frame as a genuine owner message, so the
model treated autonomous re-entries as things the USER just said — one of the
drivers of the owner-chat confusion (P1/P3). An all-forged batch now gets an
explicit AUTONOMOUS RE-ENTRY frame telling the model it is NOT user input.
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Original task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=8000,
        session_id="s-forged",
    )


def _last_text(mm):
    m = mm.history.messages[-1].message
    return m.content if isinstance(m.content, str) else str(m.content)


def test_self_wake_batch_not_framed_as_user_message():
    mm = _mm()
    mm.inject_user_guidance(
        [{"text": "continue the goal work", "kind": "self_wake", "metadata": {}}],
        session_context={"continuation": True},
    )
    text = _last_text(mm)
    assert "NEW USER MESSAGE" not in text
    assert "AUTONOMOUS RE-ENTRY" in text
    assert "continue the goal work" in text
    # must tell the model this is not the user speaking
    assert "NOT a new user message" in text


def test_genuine_message_keeps_user_frame():
    mm = _mm()
    mm.inject_user_guidance(
        [{"text": "real question", "kind": "comment", "metadata": {}}],
        session_context={"continuation": True},
    )
    text = _last_text(mm)
    assert "NEW USER MESSAGE" in text
    assert "AUTONOMOUS RE-ENTRY" not in text


def test_mixed_batch_keeps_user_frame():
    mm = _mm()
    mm.inject_user_guidance(
        [
            {"text": "wake", "kind": "self_wake", "metadata": {}},
            {"text": "real question", "kind": "comment", "metadata": {}},
        ],
        session_context={"continuation": True},
    )
    text = _last_text(mm)
    assert "NEW USER MESSAGE" in text
