"""019 P5 — TRUE token streaming (LLM_TOKEN_STREAMING, default OFF).

Adapter contract: with the flag ON and a client exposing
``astream_agent_response`` ({"type":"text"} deltas + one {"type":"final"}),
``LLMClientAdapter.astream`` yields scrubbed AIMessage deltas whose
concatenation (deltas + final chunk content) reconstructs EXACTLY what the
non-streaming path would return; tool_calls/usage/provider-response-id ride
the final chunk. Flag OFF (or no client support, or pre-first-chunk failure)
= legacy single-chunk, byte-identical.
"""
import asyncio

import pytest

from modules.llm.adapters import LLMClientAdapter
from modules.llm.messages import AIMessage, HumanMessage


class _StreamClient:
    """Fake client speaking the astream_agent_response contract."""

    model_type = "fake-model"
    supports_vision = False

    def __init__(self, text_pieces, *, tool_calls=None, usage=None,
                 response_id="resp_1", fail_immediately=False, fail_after_first=False):
        self.pieces = list(text_pieces)
        self.tool_calls = tool_calls or []
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 5,
                               "total_tokens": 15, "cached_tokens": 2}
        self.response_id = response_id
        self.fail_immediately = fail_immediately
        self.fail_after_first = fail_after_first
        self.batch_calls = 0
        self.stream_calls = 0

    async def astream_agent_response(self, messages=None, tools=None, **kwargs):
        self.stream_calls += 1
        if self.fail_immediately:
            raise RuntimeError("stream setup failed")
        for i, piece in enumerate(self.pieces):
            yield {"type": "text", "text": piece}
            if self.fail_after_first and i == 0:
                raise RuntimeError("mid-stream failure")
        yield {
            "type": "final",
            "content": "".join(self.pieces),
            "tool_calls": self.tool_calls,
            "usage_data": self.usage,
            "response_id": self.response_id,
        }

    async def generate_agent_response(self, messages=None, tools=None, **kwargs):
        self.batch_calls += 1
        return ("".join(self.pieces), self.tool_calls, self.usage)


def _collect(adapter, *, tools=None):
    async def _run():
        chunks = []
        kwargs = {"tools": tools} if tools else {}
        async for chunk in adapter.astream([HumanMessage(content="hi")], **kwargs):
            chunks.append(chunk)
        return chunks
    return asyncio.run(_run())


TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]


def test_flag_off_yields_single_chunk(monkeypatch):
    monkeypatch.delenv("LLM_TOKEN_STREAMING", raising=False)
    client = _StreamClient(["Hel", "lo"])
    adapter = LLMClientAdapter(client=client)
    chunks = _collect(adapter, tools=TOOLS)
    assert len(chunks) == 1
    assert chunks[0].content == "Hello"
    assert client.stream_calls == 0 and client.batch_calls == 1


def test_flag_on_streams_prose_deltas(monkeypatch):
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    client = _StreamClient(["Hel", "lo ", "world"])
    adapter = LLMClientAdapter(client=client)
    chunks = _collect(adapter, tools=TOOLS)
    assert client.stream_calls == 1 and client.batch_calls == 0
    assert len(chunks) > 1  # real deltas, not one blob
    full = "".join(c.content for c in chunks if isinstance(c.content, str))
    assert full == "Hello world"
    final = chunks[-1]
    assert final.usage_metadata == {
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        "cache_read_input_tokens": 2,
    }
    assert final._polyrob_provider_response_id == "resp_1"


def test_final_chunk_carries_tool_calls(monkeypatch):
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    tool_calls = [{"id": "c1", "type": "function",
                   "function": {"name": "send_message", "arguments": "{}"}}]
    client = _StreamClient(["ok"], tool_calls=tool_calls)
    adapter = LLMClientAdapter(client=client)
    chunks = _collect(adapter, tools=TOOLS)
    assert chunks[-1].tool_calls == tool_calls


def test_think_block_split_across_deltas_never_leaks(monkeypatch):
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    monkeypatch.delenv("THINK_SCRUBBER_ENABLED", raising=False)
    client = _StreamClient(["<th", "ink>secret reasoning</th", "ink>Hello"])
    adapter = LLMClientAdapter(client=client)
    chunks = _collect(adapter, tools=TOOLS)
    full = "".join(c.content for c in chunks if isinstance(c.content, str))
    assert "secret" not in full
    assert "Hello" in full


def test_brain_state_json_is_suppressed_from_live_stream(monkeypatch):
    """A completion starting with '{' (brain-state JSON) must NOT stream raw
    fragments — the final chunk carries the whole content (legacy shape)."""
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    pieces = ['{"current_state": {"memo', 'ry": "x"}}']
    client = _StreamClient(pieces)
    adapter = LLMClientAdapter(client=client)
    chunks = _collect(adapter, tools=TOOLS)
    assert len(chunks) == 1  # no live deltas escaped
    assert chunks[0].content == "".join(pieces)


def test_leading_whitespace_then_prose_still_streams(monkeypatch):
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    client = _StreamClient(["  \n", "Hi ", "there"])
    adapter = LLMClientAdapter(client=client)
    chunks = _collect(adapter, tools=TOOLS)
    full = "".join(c.content for c in chunks if isinstance(c.content, str))
    assert full == "  \nHi there"
    assert len(chunks) > 1


def test_no_client_support_falls_back_single_chunk(monkeypatch):
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")

    class _BatchOnly:
        model_type = "fake"

        async def generate_agent_response(self, messages=None, tools=None, **kwargs):
            return ("plain", [], None)

    adapter = LLMClientAdapter(client=_BatchOnly())
    chunks = _collect(adapter, tools=TOOLS)
    assert len(chunks) == 1
    assert chunks[0].content == "plain"


def test_failure_before_first_chunk_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    client = _StreamClient(["Hello"], fail_immediately=True)
    adapter = LLMClientAdapter(client=client)
    chunks = _collect(adapter, tools=TOOLS)
    assert len(chunks) == 1
    assert chunks[0].content == "Hello"  # via the batch fallback
    assert client.batch_calls == 1


def test_failure_after_first_chunk_propagates(monkeypatch):
    """After text reached the consumer a silent fallback would DOUBLE the
    streamed text — the error must propagate to the retry machinery."""
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    client = _StreamClient(["Hello ", "world"], fail_after_first=True)
    adapter = LLMClientAdapter(client=client)
    with pytest.raises(RuntimeError, match="mid-stream failure"):
        _collect(adapter, tools=TOOLS)
    assert client.batch_calls == 0


# ---------------------------------------------------------------------------
# OpenAIClient.astream_agent_response — delta assembly over a fake SDK stream
# ---------------------------------------------------------------------------


def _openai_chunk(*, content=None, tool_call=None, usage=None, chunk_id="cmpl_9"):
    from types import SimpleNamespace as NS
    delta = NS(content=content, tool_calls=[tool_call] if tool_call else None)
    choices = [] if usage is not None and content is None and tool_call is None \
        else [NS(delta=delta)]
    return NS(id=chunk_id, usage=usage, choices=choices)


def test_openai_astream_assembles_deltas_and_tool_calls(monkeypatch):
    from types import SimpleNamespace as NS

    from modules.llm.openai_client import OpenAIClient

    client = OpenAIClient.__new__(OpenAIClient)
    client._initialized = True
    client.logger = __import__("logging").getLogger("test")
    client.model_type = "gpt-4o"
    client.last_response = None
    monkeypatch.setattr(
        OpenAIClient, "_build_tool_request_params",
        lambda self, m, t, s, temp, mx, kw: {"model": "gpt-4o", "messages": [],
                                             "tools": t, "tool_choice": "auto"},
    )

    usage = NS(prompt_tokens=7, completion_tokens=3, total_tokens=10,
               prompt_tokens_details=None, completion_tokens_details=None)
    chunks_in = [
        _openai_chunk(content="Hel"),
        _openai_chunk(content="lo"),
        _openai_chunk(tool_call=NS(index=0, id="call_1",
                                   function=NS(name="send_message", arguments='{"te'))),
        _openai_chunk(tool_call=NS(index=0, id=None,
                                   function=NS(name=None, arguments='xt":"hi"}'))),
        _openai_chunk(usage=usage),
    ]

    async def _fake_stream():
        for c in chunks_in:
            yield c

    async def _create(**params):
        assert params.get("stream") is True
        assert params.get("stream_options") == {"include_usage": True}
        return _fake_stream()

    client._client = NS(chat=NS(completions=NS(create=_create)))

    async def _run():
        return [e async for e in client.astream_agent_response(
            messages=[], tools=TOOLS)]

    events = asyncio.run(_run())
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert texts == ["Hel", "lo"]
    final = events[-1]
    assert final["type"] == "final"
    assert final["content"] == "Hello"
    assert final["tool_calls"] == [{
        "id": "call_1", "type": "function",
        "function": {"name": "send_message", "arguments": '{"text":"hi"}'},
    }]
    assert final["usage_data"]["prompt_tokens"] == 7
    assert final["response_id"] == "cmpl_9"


# ---------------------------------------------------------------------------
# AnthropicClient.astream_agent_response — delta events over a fake SDK stream
# ---------------------------------------------------------------------------


def test_anthropic_astream_yields_text_deltas_and_final(monkeypatch):
    from types import SimpleNamespace as NS

    from modules.llm.anthropic_client import AnthropicClient

    client = AnthropicClient.__new__(AnthropicClient)
    client._initialized = True
    client.logger = __import__("logging").getLogger("test")
    client.model_type = "claude-sonnet-5"
    client.last_response = None
    monkeypatch.setattr(
        AnthropicClient, "_build_tool_api_params",
        lambda self, m, t, s, temp, mx, kw: {"model": "claude-sonnet-5",
                                             "messages": [], "max_tokens": 4096,
                                             "tools": t, "tool_choice": {"type": "auto"}},
    )
    monkeypatch.setattr(
        AnthropicClient, "_extract_usage_and_capture_telemetry",
        lambda self, *a, **kw: None,
    )

    final_message = NS(
        id="msg_7",
        content=[
            NS(type="text", text="Hello world"),
            NS(type="tool_use", id="tu_1", name="send_message", input={"text": "hi"}),
        ],
        usage=NS(input_tokens=11, output_tokens=4,
                 cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )

    events_in = [
        NS(type="content_block_delta", delta=NS(type="text_delta", text="Hello ")),
        NS(type="content_block_delta", delta=NS(type="input_json_delta", partial_json='{"te')),
        NS(type="content_block_delta", delta=NS(type="text_delta", text="world")),
    ]

    class _FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            async def _gen():
                for e in events_in:
                    yield e
            return _gen()

        async def get_final_message(self):
            return final_message

    client._client = NS(messages=NS(stream=lambda **params: _FakeStream()))

    async def _run():
        return [e async for e in client.astream_agent_response(
            messages=[], tools=TOOLS)]

    events = asyncio.run(_run())
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert texts == ["Hello ", "world"]
    final = events[-1]
    assert final["type"] == "final"
    assert final["content"] == "Hello world"
    assert final["tool_calls"][0]["function"]["name"] == "send_message"
    assert final["usage_data"]["prompt_tokens"] == 11
    assert final["response_id"] == "msg_7"
    assert client.last_response is final_message


# ---------------------------------------------------------------------------
# Review-fix regressions (2026-07-19)
# ---------------------------------------------------------------------------


def test_fenced_json_at_start_is_suppressed(monkeypatch):
    """A completion starting with a ``` fence must not stream live (the fenced
    blob may embed brain-state JSON) — final chunk carries the whole content."""
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    pieces = ["```json\n{\"current_state\"", ": {\"memory\": \"x\"}}\n```"]
    client = _StreamClient(pieces)
    adapter = LLMClientAdapter(client=client)
    chunks = _collect(adapter, tools=TOOLS)
    assert len(chunks) == 1
    assert chunks[0].content == "".join(pieces)


def test_trailing_brain_block_mutes_live_stream(monkeypatch):
    """Prose followed by a trailing brain block: the prose streams live, the
    brain marker mutes further deltas, and concatenation still reconstructs
    the full completion (remainder arrives as ONE final piece)."""
    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    pieces = ["Here is your answer. ", "Done!\n", '{"current_state"',
              ': {"memory": "secret"}}']
    client = _StreamClient(pieces)
    adapter = LLMClientAdapter(client=client)
    chunks = _collect(adapter, tools=TOOLS)
    full = "".join(c.content for c in chunks if isinstance(c.content, str))
    assert full == "".join(pieces)  # nothing lost
    # the brain fragment never streamed as its own live delta: every chunk
    # BEFORE the final one is brain-free
    for c in chunks[:-1]:
        assert "current_state" not in c.content
    # and the final chunk carries the withheld remainder as one piece
    assert "secret" in chunks[-1].content


def test_openai_generate_with_tools_batch_path_works(monkeypatch):
    """Review-fix regression: the P5 extraction left a stale
    `formatted_messages` reference in _generate_with_tools that NameError'd
    EVERY OpenAI batch tool call. Drive the real method with a fake SDK."""
    from types import SimpleNamespace as NS

    from modules.llm.openai_client import OpenAIClient

    client = OpenAIClient.__new__(OpenAIClient)
    client._initialized = True
    client.logger = __import__("logging").getLogger("test")
    client.model_type = "gpt-4o"
    client.temperature = 0.2
    client.max_tokens = 512
    client.last_response = None
    monkeypatch.setattr(OpenAIClient, "_adjust_max_tokens",
                        lambda self, **kw: 512)
    monkeypatch.setattr(OpenAIClient, "_validate_tool_call_pairs",
                        lambda self, msgs: None)
    monkeypatch.setattr(OpenAIClient, "_stable_prompt_cache_key",
                        lambda self, system: None)

    message = NS(content="hi there", tool_calls=[
        NS(id="c1", type="function",
           function=NS(name="send_message", arguments='{"text":"hi"}')),
    ])
    usage = NS(prompt_tokens=5, completion_tokens=2, total_tokens=7,
               prompt_tokens_details=None, completion_tokens_details=None)
    response = NS(choices=[NS(message=message)], usage=usage, id="cmpl_1")

    async def _create(**params):
        assert "stream" not in params  # batch path
        return response

    client._client = NS(chat=NS(completions=NS(create=_create)))

    async def _run():
        return await client._generate_with_tools(
            messages=[{"role": "user", "content": "hello"}],
            tools=TOOLS,
        )

    content, tool_calls, usage_data = asyncio.run(_run())
    assert content == "hi there"
    assert tool_calls[0]["function"]["name"] == "send_message"
    assert usage_data["prompt_tokens"] == 5
