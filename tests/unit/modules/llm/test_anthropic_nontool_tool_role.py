"""2026-08-17: the non-tool `_generate` path converts role='tool' messages
deliberately (labeled user text) instead of warning "Unknown role tool" per
message — 744× in the 2026-08-12..16 prod journals, one per history message on
every non-tool call over a tool-bearing conversation.
"""
import logging
from types import SimpleNamespace

import pytest

from modules.llm.anthropic_client import AnthropicClient


def _make_client(captured):
    c = object.__new__(AnthropicClient)
    c._initialized = True
    c.model_type = "claude-sonnet-4-5"
    c.temperature = 0.7
    c.logger = logging.getLogger("anthropic-nontool-tool-role")

    class _Messages:
        async def create(self, **params):
            captured.update(params)
            text_block = SimpleNamespace(type="text", text="ok")
            usage = SimpleNamespace(
                input_tokens=1, output_tokens=1,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            )
            return SimpleNamespace(content=[text_block], usage=usage)

        def stream(self, **params):
            raise AssertionError("streaming path not expected in this test")

    class _SDK:
        messages = _Messages()

    c._client = _SDK()
    return c


@pytest.mark.asyncio
async def test_tool_role_becomes_labeled_user_text(caplog):
    captured = {}
    client = _make_client(captured)
    with caplog.at_level(logging.WARNING, logger=client.logger.name):
        out = await client._generate(
            messages=[
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "calling tool"},
                {"role": "tool", "content": "42 files found",
                 "tool_call_id": "call_1"},
            ],
            system="sys", max_tokens=512,
        )
    assert out == "ok"
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["user", "assistant", "user"]
    # The client may wrap the last message in cache_control text blocks —
    # compare the carried text either way.
    content = captured["messages"][2]["content"]
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    assert content == "[tool result] 42 files found"
    assert not any("Unknown role" in r.message for r in caplog.records), (
        "role='tool' is a KNOWN role on this path now — no warning spam")


@pytest.mark.asyncio
async def test_genuinely_unknown_role_still_warns(caplog):
    captured = {}
    client = _make_client(captured)
    with caplog.at_level(logging.WARNING, logger=client.logger.name):
        await client._generate(
            messages=[{"role": "narrator", "content": "meanwhile…"}],
            system="sys", max_tokens=512,
        )
    assert any("Unknown role" in r.message for r in caplog.records)
    assert captured["messages"][0]["role"] == "user"
