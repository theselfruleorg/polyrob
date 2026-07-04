"""B4 regression: recover Kimi tool calls that leak as XML / python-call text.

kimi-k2.6 on NVIDIA NIM intermittently emits the agent's tool call as raw text
in two NON-pipe-token shapes (captured live 2026-06-16):
  A. Anthropic XML:  <invoke name="done"><parameter name="text">hi</parameter></invoke>
  B. python-call:    done(text="hi")
Left unrecovered the action is lost AND the raw brain JSON + call text dumps to
the user. recover_textual_tool_calls reconciles both shapes.
"""

from __future__ import annotations

import json

from modules.llm.openrouter_client import (
    recover_textual_tool_calls,
    tool_names_from_schemas,
)

_KNOWN = {"done", "send_message", "filesystem_write_file"}

# Verbatim-shaped leak #1 (session greeting): brain JSON wrapper + a trailing
# done(text="...") python-call. The reply text contains commas, an em-dash and
# an apostrophe — all inside the double-quoted value.
_LEAK_PYCALL = (
    '{"current_state": {"evaluation_previous_goal": "Success", '
    '"memory": "User greeted me.", "next_goal": "Reply.", '
    '"reasoning": "Be friendly.", "phase": "discovery"}} '
    'done(text="Hey! I\'m doing well, thanks for asking—what can I do for you?")'
)


def test_recovers_done_pycall_and_strips_it():
    cleaned, calls = recover_textual_tool_calls(_LEAK_PYCALL, _KNOWN)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "done"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["text"].startswith("Hey! I'm doing well")
    assert "—what can I do for you?" in args["text"]
    # The python-call text is stripped; the brain JSON survives for extraction.
    assert "done(text=" not in cleaned
    assert cleaned.startswith('{"current_state"')
    assert cleaned.rstrip().endswith("}}")


def test_recovers_invoke_xml_block():
    content = (
        "Some brain prose.\n"
        '<invoke name="done"><parameter name="text">All set.</parameter></invoke>'
    )
    cleaned, calls = recover_textual_tool_calls(content, _KNOWN)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "done"
    assert json.loads(calls[0]["function"]["arguments"]) == {"text": "All set."}
    assert "<invoke" not in cleaned and "</invoke>" not in cleaned


def test_strips_stray_partial_xml_tag_with_no_recoverable_call():
    # Leak #2: brain JSON + a lone </invoke> fragment (no recoverable call).
    content = '{"memory": "x", "next_goal": "y"} </invoke>'
    cleaned, calls = recover_textual_tool_calls(content, _KNOWN)
    assert calls == []
    assert "</invoke>" not in cleaned
    assert cleaned.startswith('{"memory"')


def test_json_args_shape_pycall():
    content = 'done({"text": "done via json args"})'
    cleaned, calls = recover_textual_tool_calls(content, _KNOWN)
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"text": "done via json args"}
    assert cleaned == ""


def test_unknown_name_is_not_recovered():
    # 'frobnicate' is not a known tool — must NOT be treated as a call.
    content = "I will frobnicate(x=1) the thing."
    cleaned, calls = recover_textual_tool_calls(content, _KNOWN)
    assert calls == []
    assert cleaned == content


def test_tool_name_inside_brain_json_is_not_spuriously_recovered():
    # 'done(' appears INSIDE the brain JSON string — must not fire or corrupt JSON.
    content = (
        '{"reasoning": "Next I will call done(text=\\"hi\\") to finish.", '
        '"next_goal": "finish", "memory": "m"}'
    )
    cleaned, calls = recover_textual_tool_calls(content, _KNOWN)
    assert calls == []
    assert cleaned == content


def test_no_op_for_plain_content():
    assert recover_textual_tool_calls("just a normal answer", _KNOWN) == (
        "just a normal answer",
        [],
    )
    assert recover_textual_tool_calls("", _KNOWN) == ("", [])


def test_tool_names_from_schemas():
    tools = [
        {"type": "function", "function": {"name": "done"}},
        {"type": "function", "function": {"name": "send_message"}},
        {"name": "bare_name"},
        "not-a-dict",
    ]
    assert tool_names_from_schemas(tools) == {"done", "send_message", "bare_name"}
    assert tool_names_from_schemas(None) == set()
