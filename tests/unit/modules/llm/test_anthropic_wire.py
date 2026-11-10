"""The Anthropic wire, with mocks only. No Anthropic key exists in this tree: none of this
has live proof."""
import json
import logging
from types import SimpleNamespace

import pytest

from core.exceptions import LLMPermanentError, LLMRefusalError
from modules.llm.adapters import AnthropicAdapter
from modules.llm.anthropic_client import AnthropicClient
from modules.llm.anthropic_images import convert_image_to_anthropic_format
from modules.llm.anthropic_wire import thinking_record, wire_messages
from modules.llm.cache_hints import conversation_breakpoints, mark_conversation_cache
from modules.llm.call_scope import call_scope
from modules.llm.compat_anthropic import AnthropicCompatClient
from modules.llm.deferred_tools import DEFERRED_TOOLS_BETA, prepare_deferred_tools
from modules.llm.messages import AIMessage, HumanMessage, ToolMessage
from modules.llm.call_result import CallFacts, output_was_truncated

TOOLS = [{"name": "t", "input_schema": {"type": "object"}}]
SIGNED = SimpleNamespace(type="thinking", thinking="", signature="sig-1")
USE = SimpleNamespace(type="tool_use", id="u1", name="t", input={"a": 1})


def _usage():
    return SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)


def _response(*content, stop_reason="end_turn", **extra):
    return SimpleNamespace(content=list(content), usage=_usage(), stop_reason=stop_reason, id="m", **extra)


def _client(model="claude-sonnet-5-5", cls=AnthropicClient, responses=None, seen=None):
    client = object.__new__(cls)
    client._initialized = True
    client.model_type = model
    client.temperature = None
    client.max_tokens = 1024
    client.last_response = None
    client.logger = logging.getLogger("wire-test")
    replies = list(responses or [_response(SimpleNamespace(type="text", text="ok"))])

    class _Messages:
        async def create(self, **params):
            if seen is not None:
                seen.append(params)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    client._client = SimpleNamespace(messages=_Messages())
    return client


def _bad_request(text):
    error = RuntimeError(text)
    error.status_code = 400
    return error


# A5 ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_refusal_raises_typed_with_the_category_on_the_tool_path():
    refusal = _response(stop_reason="refusal", stop_details=SimpleNamespace(type="refusal", category="cyber"))
    client = _client(responses=[refusal])
    with pytest.raises(LLMRefusalError, match="category: cyber") as err:
        await client.generate_agent_response([{"role": "user", "content": "x"}], TOOLS, max_tokens=16)
    assert err.value.category == "cyber"


@pytest.mark.asyncio
async def test_a_refusal_raises_typed_on_the_plain_path():
    client = _client(responses=[_response(stop_reason="refusal", stop_details={"category": "bio"})])
    with pytest.raises(LLMPermanentError, match="category: bio"):
        await client.generate_response(messages=[{"role": "user", "content": "x"}], max_tokens=16)


@pytest.mark.asyncio
async def test_a_refusal_makes_one_call():
    seen = []
    refusal = _response(stop_reason="refusal", stop_details=None)
    client = _client(responses=[refusal], seen=seen)
    with pytest.raises(LLMPermanentError):
        await client.generate_agent_response([{"role": "user", "content": "x"}], TOOLS, max_tokens=16)
    assert len(seen) == 1 and "fallbacks" not in seen[0]


# A8 ---------------------------------------------------------------------------------

def test_a_row_with_an_effort_sends_output_config():
    params = _client("claude-opus-5-5")._build_tool_api_params(
        [{"role": "user", "content": "x"}], TOOLS, "sys", None, 16, {})
    assert params["output_config"] == {"effort": "medium"}


def test_a_row_with_no_effort_sends_none():
    params = _client("claude-opus-5")._build_tool_api_params(
        [{"role": "user", "content": "x"}], TOOLS, "sys", None, 16, {})
    assert "output_config" not in params and "thinking" not in params


def test_haiku_5_5_never_gets_a_budget(monkeypatch):
    monkeypatch.setenv("THINKING_CONFIG_ENABLED", "true")
    params = _client("claude-haiku-5-5")._build_tool_api_params(
        [{"role": "user", "content": "x"}], TOOLS, "sys", None, 16, {})
    assert params["thinking"]["type"] == "adaptive" and "budget_tokens" not in params["thinking"]


def test_a_compat_seat_gets_no_thinking_control():
    client = _client("claude-sonnet-5-5", cls=AnthropicCompatClient)
    params = client._build_tool_api_params(
        [{"role": "user", "content": "x"}], TOOLS, "sys", None, 16, {})
    assert "thinking" not in params and "output_config" not in params and "extra_headers" not in params


# A4 ---------------------------------------------------------------------------------

def test_the_blocks_keep_their_position_in_the_record():
    later = SimpleNamespace(type="thinking", thinking="", signature="sig-2")
    second = SimpleNamespace(type="tool_use", id="u2", name="t", input={})
    redacted = SimpleNamespace(type="redacted_thinking", data="blob")
    record = thinking_record(_response(SIGNED, redacted, USE, later, second))
    assert record == [
        {"block": {"type": "thinking", "thinking": "", "signature": "sig-1"}, "before_tool": "u1"},
        {"block": {"type": "redacted_thinking", "data": "blob"}, "before_tool": "u1"},
        {"block": {"type": "thinking", "thinking": "", "signature": "sig-2"}, "before_tool": "u2"},
    ]


def test_no_block_makes_no_record():
    assert thinking_record(_response(USE)) is None


def _adapter(client):
    return AnthropicAdapter(client=client, model_name=client.model_type)


def test_a_block_replays_unchanged_before_its_tool_use():
    client = _client("claude-opus-5-5")
    adapter = _adapter(client)
    response = _response(SIGNED, USE)
    message = AIMessage(content="", tool_calls=[{"id": "u1", "type": "function",
                                                  "function": {"name": "t", "arguments": '{"a":1}'}}])
    adapter._stamp_reasoning(message, response)
    wire = adapter._convert_to_client_messages(
        [HumanMessage(content="go"), message, ToolMessage(content="r", tool_call_id="u1")])
    params = client._build_tool_api_params(wire, TOOLS, "sys", None, 16, {})
    blocks = params["messages"][1]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}
    assert blocks[1]["type"] == "tool_use" and blocks[1]["input"] == {"a": 1}


def test_a_changed_assistant_text_keeps_the_first_block():
    client = _client("claude-opus-5-5")
    adapter = _adapter(client)
    message = AIMessage(content="Processing actions", tool_calls=[
        {"id": "u1", "name": "t", "args": {"a": 1}}])
    adapter._stamp_reasoning(message, _response(SIGNED, SimpleNamespace(type="text", text="raw"), USE))
    wire = adapter._convert_to_client_messages([HumanMessage(content="go"), message])
    rows, _ = wire_messages(wire, None, tool_blocks=True, keep_thinking=True)
    kinds = [b["type"] for b in rows[1]["content"]]
    assert kinds == ["thinking", "text", "tool_use"] and rows[1]["content"][1]["text"] == "Processing actions"


def test_a_compat_seat_keeps_no_block():
    client = _client("glm-5", cls=AnthropicCompatClient)
    message = AIMessage(content="x")
    _adapter(client)._stamp_reasoning(message, _response(SIGNED, USE))
    assert "anthropic_thinking" not in message.additional_kwargs


def test_a_row_with_the_history_check_asks_for_drop_block():
    params = _client("claude-sonnet-5-5")._build_tool_api_params(
        [{"role": "user", "content": "x"}], TOOLS, "sys", None, 16, {})
    assert params["thinking"] == {"type": "adaptive",
                                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert "thinking-binding-controls-2026-08-01" in params["extra_headers"]["anthropic-beta"]


@pytest.mark.parametrize("text", [
    "messages.1.content.0: Invalid `signature` in `thinking` block.",
    "thinking.block_binding: Extra inputs are not permitted",
    "output_config.effort: unknown level",
])
@pytest.mark.asyncio
async def test_a_refused_control_retries_once_with_none_and_stays_off(text):
    seen = []
    reply = _response(SimpleNamespace(type="text", text="ok"))
    client = _client("claude-opus-5-5", responses=[_bad_request(text), reply, reply], seen=seen)
    message = AIMessage(content="", tool_calls=[{"id": "u1", "name": "t", "args": {}}])
    adapter = _adapter(client)
    adapter._stamp_reasoning(message, _response(SIGNED, USE))
    wire = adapter._convert_to_client_messages(
        [HumanMessage(content="go"), message, ToolMessage(content="r", tool_call_id="u1")])
    await client.generate_agent_response(wire, TOOLS, max_tokens=16)
    assert len(seen) == 2
    retry = seen[1]
    assert "thinking" not in retry and "output_config" not in retry
    assert all(b["type"] != "thinking" for b in retry["messages"][1]["content"])
    await client.generate_agent_response(wire, TOOLS, max_tokens=16)
    assert "thinking" not in seen[2] and all(b["type"] != "thinking" for b in seen[2]["messages"][1]["content"])


def test_a_marker_never_lands_on_a_thinking_block():
    rows = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "a", "name": "t", "input": {}},
        {"type": "thinking", "thinking": "", "signature": "s"}]}]
    out = mark_conversation_cache(rows, foundation_len=None)
    assert "cache_control" not in out[0]["content"][1]
    assert out[0]["content"][0]["cache_control"]


def test_a_row_of_thinking_only_gets_no_marker():
    rows = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "s"}]}]
    assert conversation_breakpoints(rows) == []


# A9 ---------------------------------------------------------------------------------

def test_parallel_results_share_one_user_message():
    rows, _ = wire_messages([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "t", "args": {}},
                                                            {"id": "b", "name": "t", "args": {}}]},
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
        {"role": "user", "content": "state"},
    ], None, tool_blocks=True)
    assert [r["role"] for r in rows] == ["assistant", "user", "user"]
    assert [b["tool_use_id"] for b in rows[1]["content"]] == ["a", "b"]


def test_the_merge_comes_before_the_markers():
    client = _client("claude-opus-5")
    params = client._build_tool_api_params([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "t", "args": {}},
                                                            {"id": "b", "name": "t", "args": {}}]},
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ], TOOLS, "sys", None, 16, {})
    tail = params["messages"][-1]["content"]
    assert len(tail) == 2 and "cache_control" in tail[1] and "cache_control" not in tail[0]


# A10 --------------------------------------------------------------------------------

def _transaction(i):
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": f"u{i}", "name": "t", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"u{i}", "content": "r"}]}]


def test_the_markers_are_the_foundation_and_the_last_two_transactions():
    rows = [{"role": "user", "content": "f0"}, {"role": "user", "content": "f1"}]
    for i in range(4):
        rows += _transaction(i)
    rows.append({"role": "user", "content": "state row"})
    assert conversation_breakpoints(rows, foundation_len=2) == [1, 7, 9]


def test_the_state_row_gets_no_marker_on_the_first_step():
    rows = [{"role": "user", "content": "f0"}, {"role": "user", "content": "state row"}]
    assert conversation_breakpoints(rows, foundation_len=1) == [0]


def test_a_chat_with_no_tools_marks_settled_turns_only():
    rows = [{"role": "user", "content": "f0"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"}, {"role": "user", "content": "new turn"},
            {"role": "user", "content": "state row"}]
    assert conversation_breakpoints(rows, foundation_len=1) == [0, 1, 2]


@pytest.mark.parametrize("content", ["", "   ", [{"type": "text", "text": ""}], []])
def test_an_empty_row_gets_no_marker(content):
    rows = [{"role": "user", "content": "x"}, {"role": "assistant", "content": content}]
    assert 1 not in conversation_breakpoints(rows)
    out = mark_conversation_cache(rows)
    assert "cache_control" not in json.dumps(out[1])


def test_a_foundation_that_ends_in_an_empty_row_marks_the_row_before():
    rows = [{"role": "user", "content": "f0"}, {"role": "user", "content": ""}]
    assert conversation_breakpoints(rows, foundation_len=2) == [0]


def test_an_addition_system_row_gets_no_marker():
    rows = [{"role": "user", "content": "x"},
            {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_reference", "name": "t"}}]}]
    assert conversation_breakpoints(rows) == [0]


# A11 and A16 ------------------------------------------------------------------------

ENV_A = "<tool-addition>\nalpha\n</tool-addition>"
ENV_B = "<tool-addition>\nbeta\n</tool-addition>"


def _deferred(messages):
    tools = [{"name": "t", "input_schema": {}}, {"name": "alpha", "defer_loading": True, "input_schema": {}}]
    return prepare_deferred_tools({"messages": messages, "tools": tools}, enabled=True)


def _names(row):
    return [b["tool"]["name"] for b in row["content"]]


def test_an_addition_stays_before_the_first_use_of_its_tool():
    result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "r"}]}
    first = [{"role": "user", "content": "go"}, result, {"role": "user", "content": ENV_A},
             {"role": "user", "content": "state"}]
    out = _deferred(first)["messages"]
    assert out[-1]["role"] == "system" and _names(out[-1]) == ["alpha"]

    later = [{"role": "user", "content": "go"}, result, {"role": "user", "content": ENV_A},
             {"role": "assistant", "content": [{"type": "tool_use", "id": "u", "name": "alpha", "input": {}}]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u", "content": "r"}]},
             {"role": "user", "content": "state 2"}]
    out = _deferred(later)["messages"]
    roles = [r["role"] for r in out]
    assert roles.index("system") == 2 and roles[3] == "assistant"
    assert out[-1]["content"] == "state 2"


def test_an_addition_does_not_move_between_two_later_steps():
    head = [{"role": "user", "content": "go"}, {"role": "user", "content": ENV_A},
            {"role": "assistant", "content": "a"}]
    one = _deferred(head + [{"role": "user", "content": "state 1"}])["messages"]
    two = _deferred(head + [{"role": "user", "content": "state 2"},
                            {"role": "assistant", "content": "b"}])["messages"]
    assert one[:3] == two[:3] and one[1]["role"] == "system"


def test_two_adjacent_trailing_envelopes_share_one_message():
    out = _deferred([{"role": "user", "content": "go"}, {"role": "user", "content": ENV_A},
                     {"role": "user", "content": ENV_B}])
    assert _names(out["messages"][-1]) == ["alpha", "beta"]
    assert sum(r["role"] == "system" for r in out["messages"]) == 1
    assert DEFERRED_TOOLS_BETA in out["extra_headers"]["anthropic-beta"]


@pytest.mark.asyncio
async def test_the_plain_path_strips_an_envelope():
    seen = []
    client = _client("claude-opus-5-5", seen=seen)
    await client._generate([{"role": "user", "content": "go"}, {"role": "user", "content": ENV_A}], max_tokens=16)
    assert ENV_A not in json.dumps(seen[0]["messages"])
    assert DEFERRED_TOOLS_BETA not in json.dumps(seen[0].get("extra_headers") or {})


def test_a_recovered_system_prompt_counts_once():
    client = _client("claude-opus-5")
    from modules.llm.token_counter import count_messages_tokens
    system = "word " * 400
    with_row = client._estimate_input_tokens(
        [{"role": "system", "content": system}, {"role": "user", "content": "x"}], system)
    single = count_messages_tokens(
        [{"role": "system", "content": system}, {"role": "user", "content": "x"}], client.model_type)
    assert with_row == single


@pytest.mark.parametrize("call,expected", [
    ({"id": "a", "function": {"name": "t", "arguments": "not json"}}, {"raw": "not json"}),
    ({"id": "a", "function": {"name": "t", "arguments": ""}}, {}),
    ({"id": "a", "name": "t", "args": {"k": 1}}, {"k": 1}),
    ({"id": "a", "function": {"name": "t", "arguments": {"k": 2}}}, {"k": 2}),
    ({"id": "a", "function": {"name": "t", "arguments": "[1]"}}, {"value": [1]}),
])
def test_tool_call_arguments_never_raise(call, expected):
    rows, _ = wire_messages([{"role": "assistant", "content": "", "tool_calls": [call]}], None, tool_blocks=True)
    block = rows[0]["content"][-1]
    assert block["name"] == "t" and block["input"] == expected


def test_the_no_tools_shape_holds_no_tool_use_and_no_empty_text():
    rows, _ = wire_messages([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "t", "args": {"k": 1}}]},
        {"role": "tool", "tool_call_id": "a", "content": "done"}], None, tool_blocks=False)
    assert rows[0]["content"] == '[called t with {"k": 1}]'
    assert rows[1]["content"] == "[tool result] done"


def test_an_assistant_row_with_no_content_leaves():
    rows, _ = wire_messages([{"role": "user", "content": "x"}, {"role": "assistant", "content": ""}],
                            None, tool_blocks=True)
    assert [r["role"] for r in rows] == ["user"]


def test_the_telemetry_metadata_stays_off_the_wire():
    params = _client("claude-opus-5")._build_tool_api_params(
        [{"role": "user", "content": "x"}], TOOLS, "sys", None, 16,
        {"metadata": {"session_id": "s1", "agent_id": "a1"}})
    assert "metadata" not in params
    params = _client("claude-opus-5")._build_tool_api_params(
        [{"role": "user", "content": "x"}], TOOLS, "sys", None, 16,
        {"metadata": {"session_id": "s1", "user_id": "u1"}})
    assert params["metadata"] == {"user_id": "u1"}


def test_the_call_scope_timeout_reaches_the_request():
    with call_scope(timeout=9.0):
        params = _client("claude-opus-5")._build_tool_api_params(
            [{"role": "user", "content": "x"}], TOOLS, "sys", None, 16, {})
    assert params["timeout"] == 9.0


def _setup_kwargs(monkeypatch, client):
    import anthropic
    seen = {}

    class _SDK:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _SDK)
    return seen


@pytest.mark.asyncio
async def test_the_sdk_limits_reach_both_clients(monkeypatch):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "33")
    client = _client("claude-opus-5")
    client.api_key = "k"
    client._resolve_profile_base_url = lambda provider: None
    seen = _setup_kwargs(monkeypatch, client)
    await client._setup_client()
    assert seen["timeout"] == 33.0 and seen["max_retries"] == AnthropicClient.DEFAULT_MAX_RETRIES

    compat = _client("glm-5", cls=AnthropicCompatClient)
    compat.api_key = "k"
    compat._spec = SimpleNamespace(bearer_auth=True, headers=lambda: {}, resolved_base_url=lambda api_key=None: None,
                                   display_name="seat")
    seen = _setup_kwargs(monkeypatch, compat)
    import anthropic
    monkeypatch.setattr(anthropic, "DefaultAsyncHttpxClient", lambda **kw: None, raising=False)
    await compat._setup_client()
    assert seen["timeout"] == 33.0


def test_tools_count_in_the_output_clamp():
    calls = {}
    client = _client("claude-opus-5")
    real = client._adjust_max_tokens

    def spy(**kwargs):
        calls.update(kwargs)
        return real(**kwargs)

    client._adjust_max_tokens = spy
    client._build_tool_api_params([{"role": "user", "content": "x"}], TOOLS, "sys", None, 16, {})
    assert calls["tools"] is TOOLS


# A13 and A15 ------------------------------------------------------------------------

def test_a_context_window_stop_reads_as_truncated():
    assert output_was_truncated(CallFacts(finish_reason="model_context_window_exceeded"))


@pytest.mark.parametrize("media", ["image/png", "image/jpeg", "image/gif", "image/webp", "IMAGE/PNG"])
def test_an_allowed_media_type_passes(media):
    out = convert_image_to_anthropic_format(
        {"type": "image_url", "image_url": {"url": f"data:{media};base64,AAAA"}})
    assert out["type"] == "image" and out["source"]["media_type"] == media.lower()


@pytest.mark.parametrize("url", [
    "data:image/svg+xml;base64,AAAA",
    "data:application/pdf;base64,AAAA",
    "data:image/png,rawbytes",
])
def test_a_refused_image_names_itself_as_text(url):
    out = convert_image_to_anthropic_format({"type": "image_url", "image_url": {"url": url}})
    assert out["type"] == "text" and out["text"].startswith("[IMAGE:")


def test_the_sdk_floor_is_1_6():
    import pathlib
    text = pathlib.Path(__file__).resolve().parents[4].joinpath("pyproject.toml").read_text()
    assert 'anthropic = ["anthropic>=1.6"]' in text


def test_a_thinking_controls_refusal_expires(monkeypatch):
    """One session's 400 must not turn thinking off for every session for the process."""
    from types import SimpleNamespace as NS
    from modules.llm import anthropic_wire as aw
    now = [500.0]
    monkeypatch.setattr(aw.time, "monotonic", lambda: now[0])
    client = NS()
    exc = type("BadRequestError", (Exception,), {"status_code": 400})("a `thinking` block is invalid")
    out = aw.retry_params_without_thinking_controls(
        client, exc, {"thinking": {"type": "adaptive"}, "messages": []})
    assert out is not None and "thinking" not in out
    assert aw.thinking_controls_unavailable(client)
    now[0] += aw.LATCH_TTL_SEC + 1
    assert not aw.thinking_controls_unavailable(client)
