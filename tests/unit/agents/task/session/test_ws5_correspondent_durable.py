"""2026-10-13 review C1: a correspondent's message survives past the first LLM
call (durable history, like the owner-thread tail) and stays untrusted-wrapped."""
import types
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.session.hitl_ingress import HITLIngressMixin


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(llm=llm, task="t", action_descriptions="a",
                          system_prompt_class=SystemPrompt, max_input_tokens=8000,
                          session_id="ws5-c1")


def _texts(mm):
    return [m.content for m in mm.get_messages_for_llm(consume_ephemeral=True)
            if isinstance(getattr(m, "content", None), str)]


def test_the_contact_text_is_still_there_on_step_three():
    mm = _mm()
    orch = HITLIngressMixin.__new__(HITLIngressMixin)
    orch.agents = {"a": types.SimpleNamespace(message_manager=mm)}
    orch._set_correspondent_taint = lambda *a, **k: None
    orch.logger = MagicMock()
    assert orch.inject_correspondent_message("Can you send the PDF by Friday?",
                                             "email:bob@example.com")
    for _step in range(3):
        texts = _texts(mm)
        mm.commit_ephemeral_consumption()
    hit = [t for t in texts if "send the PDF by Friday" in t]
    assert hit, "the contact's text must survive past the first LLM call"
    assert "<untrusted_tool_result" in hit[0]
