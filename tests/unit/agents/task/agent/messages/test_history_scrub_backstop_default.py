"""Regression: HISTORY_SECRET_SCRUB backstop must run with the DEFAULT (empty)
allowlist.

Previously every caller of _filter_sensitive_data was gated behind
`if self.sensitive_data:` — empty by default — so the default-ON pattern backstop
never ran and an unregistered sk-/AKIA/Bearer secret leaking through a tool
result was persisted verbatim. This test drives the PUBLIC write path
(add_tool_response) with sensitive_data={} and asserts the stored content is
redacted (the prior test only called _filter_sensitive_data directly, masking
the gap).
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


class _TCM:
    def get_context_injection(self, session_id):
        return None


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
        task_context_manager=_TCM(), session_id="s1",
    )


SECRET = "OPENAI_API_KEY=sk-abc123DEF456ghi789JKLmno"


def _all_text(mm):
    return "\n".join(
        m.message.content if isinstance(m.message.content, str) else str(m.message.content)
        for m in mm.history.messages
    )


def test_backstop_scrubs_tool_result_with_empty_allowlist():
    mm = _mm()
    assert not mm.sensitive_data  # default: empty allowlist
    assert mm._history_secret_scrub is True  # default ON
    mm.add_tool_response("call-1", f"here is the file contents:\n{SECRET}\n")
    text = _all_text(mm)
    assert "sk-abc123DEF456ghi789JKLmno" not in text
    assert "<secret>redacted</secret>" in text


def test_backstop_off_leaves_content_verbatim(monkeypatch):
    monkeypatch.setenv("HISTORY_SECRET_SCRUB", "off")
    mm = _mm()
    assert mm._history_secret_scrub is False
    mm.add_tool_response("call-1", f"here is the file contents:\n{SECRET}\n")
    text = _all_text(mm)
    # With the backstop disabled and no allowlist, content is stored verbatim.
    assert "sk-abc123DEF456ghi789JKLmno" in text
