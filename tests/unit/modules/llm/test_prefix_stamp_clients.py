"""F20: every provider path stamps its prefix identity on the client.

Before this, only OpenRouter did. `usage_records` could therefore group
first-call cache hits by prefix identity on exactly one seat, and the question
"are the bytes we send the same run to run?" stayed an inference everywhere
else. Each test drives the real client method with a fake SDK object and asserts
the stamps read back through the adapter, i.e. through `llm._client` — which is
the POLYROB client wrapper, not the vendor SDK object.
"""
import asyncio
import types

import pytest

from modules.llm.prefix_stamp import read_stamps

SYSTEM = "You are Rob."
TOOLS = [{"type": "function", "function": {"name": "read_file"}}]


def _read(client):
    return read_stamps(types.SimpleNamespace(_client=client))


# ── OpenAI (and every OpenAI-compat seat that inherits this path) ───────────

class _FakeChatCompletions:
    def __init__(self, sink):
        self._sink = sink

    async def create(self, **params):
        self._sink.append(params)
        msg = types.SimpleNamespace(content="ok", tool_calls=None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                        total_tokens=2),
            id="resp-1",
        )


class _FakeSDK:
    def __init__(self, sink):
        self.chat = types.SimpleNamespace(completions=_FakeChatCompletions(sink))


def _openai_client():
    from modules.llm.openai_client import OpenAIClient
    c = OpenAIClient.__new__(OpenAIClient)
    c.model_type = "gpt-5.1"
    c.max_tokens = 128
    c.temperature = 0.7
    c.supports_vision = False
    c.last_response = None
    c.logger = types.SimpleNamespace(debug=lambda *a, **k: None,
                                     info=lambda *a, **k: None,
                                     warning=lambda *a, **k: None,
                                     error=lambda *a, **k: None,
                                     isEnabledFor=lambda *a: False)
    c._extract_usage_and_capture_telemetry = lambda *a, **k: None
    return c


def test_openai_tool_path_stamps_the_prefix():
    sink = []
    c = _openai_client()
    c._client = _FakeSDK(sink)
    asyncio.run(c._generate_with_tools(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "go"}],
        tools=TOOLS, system=None))
    stamps = _read(c)
    assert set(stamps) == {"prefix_sha", "tools_sha"}


# ── Anthropic (a top-level `system` key, not a system message) ─────────────

class _FakeMessages:
    def __init__(self, sink):
        self._sink = sink

    async def create(self, **params):
        self._sink.append(params)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="ok")],
            usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
            stop_reason="end_turn", id="msg-1",
        )


def _anthropic_client():
    from modules.llm.anthropic_client import AnthropicClient
    c = AnthropicClient.__new__(AnthropicClient)
    c.model_type = "claude-sonnet-4-5"
    c.max_tokens = 128
    c.temperature = 0.7
    c.supports_vision = False
    c.last_response = None
    c.logger = types.SimpleNamespace(debug=lambda *a, **k: None,
                                     info=lambda *a, **k: None,
                                     warning=lambda *a, **k: None,
                                     error=lambda *a, **k: None,
                                     isEnabledFor=lambda *a: False)
    c._extract_usage_and_capture_telemetry = lambda *a, **k: None
    c._initialized = True
    return c


def test_anthropic_tool_path_stamps_the_prefix():
    """The Anthropic request has no system MESSAGE — before F20 this path
    stamped nothing at all, on the seat the cache work is measured from."""
    sink = []
    c = _anthropic_client()
    c._client = types.SimpleNamespace(messages=_FakeMessages(sink))
    asyncio.run(c._generate_with_tools(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "go"}],
        tools=[{"name": "read_file", "input_schema": {}}], system=None))

    stamps = _read(c)
    assert set(stamps) == {"prefix_sha", "tools_sha"}
    # the request really did carry the system prompt as a top-level key
    assert sink and "system" in sink[0]


# ── DeepSeek (its own requests.post transport, no SDK client object) ────────

def test_deepseek_stamps_the_prefix():
    from modules.llm.prefix_stamp import stamp_client, compute_stamps
    # The DeepSeek path hands the SAME request_body dict to stamp_client that it
    # posts, so the contract is exactly compute_stamps of that body.
    body = {"model": "deepseek-chat",
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "go"}],
            "tools": TOOLS}
    client = types.SimpleNamespace()
    stamp_client(client, body)
    assert _read(client) == compute_stamps(body)
    assert set(_read(client)) == {"prefix_sha", "tools_sha"}


# ── the Responses API (`instructions`, not a system message) ────────────────

def test_responses_path_stamps_instructions_as_the_prefix():
    from modules.llm.prefix_stamp import stamp_client, compute_stamps
    params = {"model": "gpt", "input": [{"role": "user", "content": "go"}],
              "instructions": SYSTEM, "tools": TOOLS}
    client = types.SimpleNamespace()
    stamp_client(client, params, system=SYSTEM)
    assert _read(client)["prefix_sha"] == compute_stamps({"system": SYSTEM})["prefix_sha"]


# ── OpenRouter / NVIDIA / the OpenAI-compat seats ──────────────────────────

def test_openrouter_family_stamps_through_apply_request_extras():
    """NvidiaClient and OpenAICompatClient both subclass OpenRouterClient, so
    they inherit the one stamp site rather than adding a second."""
    from modules.llm.openrouter_reasoning import apply_request_extras
    from modules.llm.openrouter_client import OpenRouterClient
    from modules.llm.nvidia_client import NvidiaClient
    from modules.llm.compat_clients import OpenAICompatClient

    assert issubclass(NvidiaClient, OpenRouterClient)
    assert issubclass(OpenAICompatClient, OpenRouterClient)

    client = types.SimpleNamespace()
    apply_request_extras({"messages": [{"role": "system", "content": SYSTEM}],
                          "tools": TOOLS}, client, 8192)
    assert set(_read(client)) == {"prefix_sha", "tools_sha"}


def test_an_unstamped_path_is_silent_not_wrong():
    """Gemini is deliberately not stamped; `read_stamps` must say nothing rather
    than invent an identity."""
    assert _read(types.SimpleNamespace()) == {}
