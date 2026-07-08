"""P0-1 (intelligence-polish plan 2026-07-07): the 500-char guidance truncation
destroyed forged-turn payloads and mangled long owner input.

- A forged (self-wake / delegation-result) body is pre-wrapped; the old
  ``text[:500] + "..."`` cut delivered ZERO payload and left an UNCLOSED
  ``<untrusted_tool_result>`` tag in history.
- A long genuine user message was hard-cut at 500 chars, losing its trailing detail.
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.agent.core.self_wake import format_self_wake


def _mm():
	llm = MagicMock()
	llm.model_name = "gpt-4o"
	return MessageManager(
		llm=llm, task="Original task", action_descriptions="acts",
		system_prompt_class=SystemPrompt, max_input_tokens=8000,
		session_id="s-p0-1",
	)


def _last_text(mm):
	m = mm.history.messages[-1].message
	return m.content if isinstance(m.content, str) else str(m.content)


def test_self_wake_payload_survives_and_delimiter_closed():
	"""A 1.5KB goal result inside a self-wake reaches the agent intact and the
	untrusted-wrap block is CLOSED (regression: it used to be cut to nothing with an
	open tag)."""
	payload = "GOAL RESULT: " + "x" * 1500 + " END_OF_RESULT_MARKER"
	body = format_self_wake(payload, source="self_wake")
	mm = _mm()
	mm.inject_user_guidance(
		[{"text": body, "kind": "self_wake", "metadata": {}}],
		session_context={"continuation": True},
	)
	text = _last_text(mm)
	assert "GOAL RESULT:" in text
	assert "END_OF_RESULT_MARKER" in text, "trailing payload (and closing delimiter) must survive"
	# untrusted-wrap block is balanced
	assert text.count("<untrusted_tool_result") == text.count("</untrusted_tool_result>")
	assert text.count("</untrusted_tool_result>") >= 1


def test_format_self_wake_bounds_huge_payload_but_closes_delimiter():
	"""An oversized wake payload is head+tail elided at the source, so the wrapped
	result still closes its delimiter."""
	payload = "HEAD_MARKER " + "y" * 50000 + " TAIL_MARKER"
	body = format_self_wake(payload, source="self_wake")
	assert "HEAD_MARKER" in body
	assert "TAIL_MARKER" in body
	assert "elided" in body
	assert body.count("<untrusted_tool_result") == body.count("</untrusted_tool_result>")


def test_long_user_message_head_and_tail_preserved():
	"""A 3KB genuine user message keeps head AND tail with an elision marker (not a
	hard [:500] cut)."""
	msg = "IMPORTANT_START please do the following long task " + "z" * 3000 + " FINAL_INSTRUCTION_AT_END"
	mm = _mm()
	mm.inject_user_guidance(
		[{"text": msg, "kind": "comment", "metadata": {}}],
		session_context={"continuation": True},
	)
	text = _last_text(mm)
	assert "IMPORTANT_START" in text
	assert "FINAL_INSTRUCTION_AT_END" in text, "tail of a long user message must survive"


def test_short_message_unchanged():
	"""Short messages are passed through verbatim (no elision marker)."""
	mm = _mm()
	mm.inject_user_guidance(
		[{"text": "quick question", "kind": "comment", "metadata": {}}],
		session_context={"continuation": True},
	)
	text = _last_text(mm)
	assert "quick question" in text
	assert "chars elided" not in text
