"""Regression test: Anthropic native path must NOT drop the system prompt.

The agent builds the real system prompt as a role='system' message in the
messages list and does not pass a `system=` kwarg. Before the fix, both
_generate_with_tools and _generate did `if role == 'system': continue` and then
built system_param from the (None) kwarg, so Claude received NO system prompt.
These tests assert the in-list system content is recovered and reaches the API
request for both the tools and no-tools paths.
"""
import asyncio

import pytest

import modules.llm.anthropic_client as ac
from modules.llm.anthropic_client import AnthropicClient


class _FakeMessages:
    def __init__(self, sink):
        self._sink = sink

    async def create(self, **kwargs):
        self._sink["params"] = kwargs

        class _Resp:
            content = []
        return _Resp()


class _FakeClient:
    def __init__(self, sink):
        self.messages = _FakeMessages(sink)


def _make_client():
    c = AnthropicClient.__new__(AnthropicClient)
    c._initialized = True
    c.model_type = "claude-sonnet-4-5"
    c.temperature = 0.7
    c.max_tokens = 8192
    c.last_response = None

    import logging
    c.logger = logging.getLogger("test-anthropic")
    # Stub heavy helpers that are irrelevant to system-prompt recovery.
    c._adjust_max_tokens = lambda **kw: 1024
    c._extract_usage_and_capture_telemetry = lambda *a, **k: None
    c._extract_usage_data = lambda *a, **k: {}
    return c


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def _capture(system):
        seen["system"] = system
        return system  # keep it non-None so api_params['system'] is set

    monkeypatch.setattr(ac, "_build_cached_system_param", _capture)
    monkeypatch.setattr(ac, "count_messages_tokens", lambda *a, **k: 10)
    # thinking config off by default; don't let it interfere
    monkeypatch.setattr(ac, "_apply_conversation_cache", lambda m: m, raising=False)
    return seen


SYS = "You are ROB. Memory format: ...\n<security>...</security>"
MSGS = [
    {"role": "system", "content": SYS},
    {"role": "user", "content": "hi"},
]


def test_tools_path_recovers_system_from_message_list(captured):
    c = _make_client()
    sink = {}
    c._client = _FakeClient(sink)
    asyncio.run(
        c._generate_with_tools(MSGS, tools=[{"name": "noop", "input_schema": {}}])
    )
    assert captured["system"] == SYS
    assert sink["params"].get("system") == SYS


def test_no_tools_path_recovers_system_from_message_list(captured):
    c = _make_client()
    sink = {}
    c._client = _FakeClient(sink)
    asyncio.run(
        c._generate(MSGS)
    )
    assert captured["system"] == SYS
    assert sink["params"].get("system") == SYS


def test_explicit_system_kwarg_is_preserved(captured):
    c = _make_client()
    sink = {}
    c._client = _FakeClient(sink)
    asyncio.run(
        c._generate_with_tools(MSGS, tools=[{"name": "noop", "input_schema": {}}],
                               system="EXPLICIT")
    )
    assert captured["system"] == "EXPLICIT"
