"""Recover Kimi-K2 tool calls when the serving layer leaks the delimiter tokens.

Regression for the NVIDIA-NIM + kimi-k2.6 failure: NIM intermittently returns
Kimi's native ``<|tool_call_begin|>`` tokens as raw text in ``content`` with no
structured ``tool_calls``. The agent then loses the actions (wasted planning
turn) and dumps the tokens to the CLI. ROB recovers them client-side.
"""

from __future__ import annotations

import json

from modules.llm.openrouter_client import (
    parse_kimi_tool_calls,
    recover_kimi_content,
    strip_kimi_tokens,
)

# The verbatim leaked content from session ee5db71c step 2 (brain JSON + a stray
# end token + three unparsed read_file calls + the section-end token).
_LEAKED = (
    '{"current_state": {"evaluation_previous_goal":"Success","memory":"Explored root.",'
    '"next_goal":"Read README.md and pyproject.toml","reasoning":"Understand the project."}} '
    '<|tool_call_end|> '
    '<|tool_call_begin|> functions.filesystem_read_file:1 <|tool_call_argument_begin|> '
    '{"filePath": "README.md"} <|tool_call_end|> '
    '<|tool_call_begin|> functions.filesystem_read_file:2 <|tool_call_argument_begin|> '
    '{"filePath": "pyproject.toml"} <|tool_call_end|> '
    '<|tool_call_begin|> functions.filesystem_read_file:3 <|tool_call_argument_begin|> '
    '{"filePath": "main.py"} <|tool_call_end|> <|tool_calls_section_end|>'
)


def test_parses_all_three_leaked_tool_calls():
    calls = parse_kimi_tool_calls(_LEAKED)
    assert len(calls) == 3
    assert [c["function"]["name"] for c in calls] == ["filesystem_read_file"] * 3
    # Args are preserved verbatim (downstream validation handles field naming).
    assert json.loads(calls[0]["function"]["arguments"]) == {"filePath": "README.md"}
    assert json.loads(calls[2]["function"]["arguments"]) == {"filePath": "main.py"}
    # OpenAI tool-call shape so the existing pipeline consumes it unchanged.
    assert calls[0]["type"] == "function"
    assert "id" in calls[0]


def test_strip_keeps_brain_state_drops_token_soup():
    cleaned = strip_kimi_tokens(_LEAKED)
    assert "<|tool_call" not in cleaned
    assert cleaned.startswith('{"current_state"')
    assert cleaned.endswith("}}")


def test_no_op_for_non_kimi_content():
    assert parse_kimi_tool_calls("") == []
    assert parse_kimi_tool_calls("just a normal answer") == []
    assert parse_kimi_tool_calls('{"current_state": {"memory": "x"}}') == []
    # Plain content is returned unchanged by the stripper.
    assert strip_kimi_tokens("just a normal answer") == "just a normal answer"


def test_skips_tool_call_with_unparseable_args():
    bad = (
        "<|tool_call_begin|> functions.foo:1 <|tool_call_argument_begin|> "
        "{not valid json} <|tool_call_end|>"
    )
    assert parse_kimi_tool_calls(bad) == []


# ---------------------------------------------------------------------------
# WS-2.1: unconditional token strip (the stray-end-token shape, session c1c81b26)
# ---------------------------------------------------------------------------

# The latest leak (session c1c81b26): brain JSON + stray CLOSING tokens only — no
# <|tool_call_begin|> block. parse_kimi_tool_calls() finds 0 calls, so the old
# code never stripped and the raw tokens leaked to the user.
_STRAY_END = (
    '{"current_state": {"evaluation_previous_goal":"Success",'
    '"memory":"Reviewed the folder.","next_goal":"Deliver the review",'
    '"reasoning":"All files read."}} '
    '<|tool_call_end|> <|tool_calls_section_end|>'
)


def test_strip_handles_stray_end_tokens_with_no_begin_block():
    cleaned = strip_kimi_tokens(_STRAY_END)
    assert "<|tool_call" not in cleaned
    assert cleaned.startswith('{"current_state"')
    assert cleaned.endswith("}}")


def test_recover_strips_stray_end_tokens_even_with_zero_calls():
    # The crux of WS-2.1: 0 recovered calls but tokens MUST still be stripped.
    cleaned, calls = recover_kimi_content(_STRAY_END)
    assert calls == []
    assert "<|tool_call" not in cleaned
    assert cleaned.startswith('{"current_state"')


def test_recover_strips_and_recovers_begin_block_shape():
    cleaned, calls = recover_kimi_content(_LEAKED)
    assert len(calls) == 3
    assert "<|tool_call" not in cleaned
    assert cleaned.startswith('{"current_state"')


def test_recover_is_noop_for_clean_content():
    assert recover_kimi_content("just a normal answer") == ("just a normal answer", [])
    assert recover_kimi_content('{"current_state": {"memory": "x"}}') == (
        '{"current_state": {"memory": "x"}}',
        [],
    )
    assert recover_kimi_content("") == ("", [])


def test_kimi_parallel_calls_get_unique_ids():
    from modules.llm.openrouter_client import parse_kimi_tool_calls
    content = ('<|tool_call_begin|> functions.read:0 <|tool_call_argument_begin|> {"path":"a"} <|tool_call_end|>'
               '<|tool_call_begin|> functions.write:0 <|tool_call_argument_begin|> {"path":"b"} <|tool_call_end|>')
    calls = parse_kimi_tool_calls(content)
    assert len(calls) == 2
    ids = [c["id"] for c in calls]
    assert len(set(ids)) == 2, f"ids must be unique, got {ids}"


def test_kimi_recovers_nested_object_args():
    from modules.llm.openrouter_client import parse_kimi_tool_calls
    import json
    content = ('<|tool_call_begin|> functions.search:0 '
               '<|tool_call_argument_begin|> {"filter": {"a": 1, "b": [2,3]}} <|tool_call_end|>')
    calls = parse_kimi_tool_calls(content)
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"])["filter"] == {"a": 1, "b": [2, 3]}
