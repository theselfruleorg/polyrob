"""B2 (blocker) — normalize_tool_call must NOT corrupt a legit string argument
whose value merely looks like JSON.

_deep_parse_json_strings was applied to every already-a-dict argument value, so a
common call like write_file(content='{"name":"test"}') had its string content
turned into a dict, which then failed the WriteFileAction(content: str) validation
and was silently dropped (file never written). Fix: on the already-a-dict path,
convert protobuf types but do NOT deep-parse string values; keep deep-parse only
for the fully-stringified-args blob (the real Grok 4.1 case).
"""
from agents.task.agent.message_manager.tool_call_builder import ToolCallBuilder

norm = ToolCallBuilder.normalize_tool_call


def test_anthropic_dict_args_json_looking_string_stays_string():
    tc = {"name": "write_file",
          "input": {"file_path": "data.json", "content": '{"name": "test"}'}}
    out = norm(tc)
    assert out["args"]["content"] == '{"name": "test"}'  # NOT a dict
    assert isinstance(out["args"]["content"], str)


def test_openai_dict_args_json_array_string_stays_string():
    tc = {"function": {"name": "write_file",
                        "arguments": None}}
    # simulate the SDK already handing us a dict of args (not a JSON string)
    tc = {"id": "1", "function": {"name": "write_file"}}
    tc["function"]["arguments"] = {"file_path": "x.txt", "content": '["a", "b"]'}
    out = norm(tc)
    assert out["args"]["content"] == '["a", "b"]'
    assert isinstance(out["args"]["content"], str)


def test_fully_stringified_args_blob_still_parsed():
    # The genuine Grok case: the entire arguments field arrived as a JSON string.
    tc = {"id": "2", "function": {"name": "search",
          "arguments": '{"query": "cats", "filters": {"type": "image"}}'}}
    out = norm(tc)
    assert out["args"]["query"] == "cats"
    assert out["args"]["filters"] == {"type": "image"}  # parsed from the blob


def test_plain_scalar_args_unaffected():
    tc = {"name": "click_element", "input": {"index": 3, "text": "hello"}}
    out = norm(tc)
    assert out["args"]["index"] == 3
    assert out["args"]["text"] == "hello"
