"""C10 (2026-10-13): a mixed drain labels the runtime's part as not the owner."""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


def _frame(batch):
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    mm = MessageManager(llm=llm, task="t", action_descriptions="acts",
                        system_prompt_class=SystemPrompt, max_input_tokens=8000,
                        session_id="s-mixed")
    mm.inject_user_guidance(batch, session_context={"continuation": True})
    return mm.history.messages[-1].message.content


def test_mixed_batch_labels_forged_part():
    body = _frame([
        {"text": "use Solana", "kind": "comment", "metadata": {}},
        {"text": "<untrusted_tool_result>child said X</untrusted_tool_result>",
         "kind": "delegation_result", "metadata": {}},
    ])
    assert "use Solana" in body
    assert "delegation result from the agent runtime — not the owner" in body
    assert "[comment" not in body


def test_owner_only_batch_has_no_label():
    assert "not the owner" not in _frame([{"text": "hi", "kind": "comment", "metadata": {}}])
