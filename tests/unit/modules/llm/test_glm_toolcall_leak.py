"""GLM (z-ai) raw tool-call XML leak: strip it from user-facing content.

GLM via OpenRouter intermittently emits tool calls as a raw <function_calls> block
(tool name as tag + <arg_key>/<arg_value> children, sometimes malformed) instead of
structured tool_calls. ROB's recovery was Kimi-gated, so GLM's dump leaked to the
user. We can't reliably recover the malformed args, but we MUST strip the block.
"""
from modules.llm.openrouter_client import (
    recover_textual_tool_calls,
    _has_toolcall_xml_marker,
)


GLM_LEAK = (
    "Here is the result.\n"
    "<function_calls>\n"
    '<filesystem_write_file filePath="calc.py"><arg_key>content</arg_key>'
    "<arg_value>def add(a, b): return a - b</arg_value>\n"
    "</filesystem_write_file>\n"
    "</function_calls>"
)


def test_glm_function_calls_block_is_stripped():
    cleaned, _calls = recover_textual_tool_calls(GLM_LEAK, {"filesystem_write_file"})
    assert "<function_calls" not in cleaned
    assert "<arg_key>" not in cleaned
    assert "<filesystem_write_file" not in cleaned
    assert "Here is the result." in cleaned  # surrounding prose preserved


def test_marker_detector():
    assert _has_toolcall_xml_marker(GLM_LEAK) is True
    assert _has_toolcall_xml_marker("just normal prose, no tools") is False
    assert _has_toolcall_xml_marker('<invoke name="done">x</invoke>') is True


def test_plain_prose_untouched():
    prose = "I will analyze the project and write a summary."
    cleaned, calls = recover_textual_tool_calls(prose, {"done", "write_file"})
    assert cleaned == prose
    assert calls == []


def test_invoke_xml_still_recovered_regression():
    content = '<invoke name="done"><parameter name="text">hi</parameter></invoke>'
    cleaned, calls = recover_textual_tool_calls(content, {"done"})
    assert len(calls) == 1 and calls[0]["function"]["name"] == "done"
    assert "<invoke" not in cleaned
