"""CX-H3: `_log_context_breakdown` must peek at ephemeral messages, not drain them.

`get_messages_for_llm()` used to unconditionally clear `_ephemeral_messages` after
including them once. `_log_context_breakdown()` (called purely for logging on steps
1-3 and every 10th step) called `get_messages_for_llm()` too, so a one-shot
ephemeral (correspondent reply, deferral notice, memory-writer note) could be eaten
by a logging call before the real LLM call ever saw it.

Fix: `get_messages_for_llm(consume_ephemeral: bool = True)`. The real provider-call
site (`next_action_internal.py`) keeps the default (drains, as before). The
diagnostic-only `_log_context_breakdown` passes `consume_ephemeral=False` so the
ephemeral survives logging and is still there for the actual LLM call.
"""
from unittest.mock import MagicMock

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import HumanMessage


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
    )


def test_get_messages_for_llm_default_still_consumes():
    """Default behaviour (real LLM call path) is unchanged: drains after one use."""
    mm = _mm()
    mm.push_ephemeral_message(HumanMessage(content="one-shot"))
    assert len(mm._ephemeral_messages) == 1

    mm.get_messages_for_llm()
    assert len(mm._ephemeral_messages) == 0


def test_get_messages_for_llm_consume_false_peeks():
    """consume_ephemeral=False includes the ephemeral in output but leaves it queued."""
    mm = _mm()
    mm.push_ephemeral_message(HumanMessage(content="one-shot"))

    msgs = mm.get_messages_for_llm(consume_ephemeral=False)
    texts = [m.content if isinstance(m.content, str) else str(m.content) for m in msgs]
    assert any("one-shot" in t for t in texts)
    assert len(mm._ephemeral_messages) == 1

    # The real send-to-LLM call still drains it exactly once.
    mm.get_messages_for_llm()
    assert len(mm._ephemeral_messages) == 0


def test_log_context_breakdown_does_not_consume_ephemerals():
    """End-to-end: a logging-only breakdown call must not eat a queued ephemeral."""
    from agents.task.agent.core.logging_io import LoggingIOMixin

    class _Agent(LoggingIOMixin):
        def __init__(self, message_manager):
            self.message_manager = message_manager
            self.logger = MagicMock()

    mm = _mm()
    mm.push_ephemeral_message(HumanMessage(content="correspondent-reply"))
    agent = _Agent(mm)

    agent._log_context_breakdown()
    assert len(mm._ephemeral_messages) == 1, (
        "logging-only _log_context_breakdown must not drain ephemeral messages"
    )

    # The real LLM call still drains it exactly once.
    mm.get_messages_for_llm()
    assert len(mm._ephemeral_messages) == 0

# B4: the brittle inspect.getsource guard (asserting the literal
# "consume_ephemeral=False" appears in _log_context_breakdown's source) was
# removed — it was fragile to safe refactors and fully redundant with the
# behavioral test above (test_log_context_breakdown_does_not_consume_ephemerals),
# which pins the actual peek-not-drain contract end-to-end.
