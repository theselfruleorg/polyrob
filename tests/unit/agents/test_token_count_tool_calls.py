"""CX-H4: tool-call arguments must be counted as real prompt bytes.

`_count_message_tokens` built `{"role": message.type, "content": message.content}`
and never included `tool_calls` — though `modules.llm.token_counter.count_messages_tokens`
already supports counting a `tool_calls` key (`json.dumps(message['tool_calls'])`). An
AIMessage carrying a large tool argument (e.g. a 40KB file write) counted as ~nothing,
so compaction thresholds fired late.
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import AIMessage


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Original task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=8000,
        session_id="s-toolcalls",
    )


def test_tool_call_args_are_counted():
    mm = _mm()
    big = AIMessage(content="", tool_calls=[{
        "id": "t1", "name": "filesystem_write_file",
        "args": {"content": "x" * 40000, "file_path": "a.txt"},
    }])
    small = AIMessage(content="", tool_calls=[{"id": "t2", "name": "done", "args": {}}])
    assert mm._count_message_tokens(big) > mm._count_message_tokens(small) + 5000


def test_estimate_tokens_also_counts_tool_calls():
    mm = _mm()
    big = AIMessage(content="", tool_calls=[{
        "id": "t1", "name": "filesystem_write_file",
        "args": {"content": "y" * 40000, "file_path": "b.txt"},
    }])
    small = AIMessage(content="", tool_calls=[{"id": "t2", "name": "done", "args": {}}])
    assert mm.estimate_tokens([big]) > mm.estimate_tokens([small]) + 5000


def test_tool_call_args_counted_in_exception_fallback(monkeypatch):
    """When the downstream count_messages_tokens raises, the len(json.dumps(tool_calls))
    fallback must still count a big tool argument as big (not ~nothing)."""
    import agents.task.agent.messages.token_counter as tc

    def _boom(*_a, **_k):
        raise RuntimeError("counter unavailable")

    # Force both the per-message and the batch counter down the fallback branch.
    monkeypatch.setattr(tc, "count_messages_tokens", _boom)

    mm = _mm()
    big = AIMessage(content="", tool_calls=[{
        "id": "t1", "name": "filesystem_write_file",
        "args": {"content": "z" * 40000, "file_path": "c.txt"},
    }])
    small = AIMessage(content="", tool_calls=[{"id": "t2", "name": "done", "args": {}}])

    # Fallback path: ~40KB of JSON arg -> ~10k tokens, dwarfing the tiny call.
    assert mm._count_message_tokens(big) > mm._count_message_tokens(small) + 5000
