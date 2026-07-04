"""H2: the structured-output fallback had two duplicated streaming blocks that drifted —
one accumulated usage_metadata correctly, the other referenced an UNDEFINED usage_metadata
(NameError every time that branch streamed, on non-native-tool-calling providers). Both
now route through one _stream_plain_fallback helper; this tests it collects content +
usage and never references an undefined name.
"""
import asyncio
from types import SimpleNamespace

from agents.task.agent.service import Agent


def _agent(chunks):
    agent = Agent.__new__(Agent)

    async def astream(_messages):
        for c in chunks:
            yield c

    streamed = []

    class _Hitl:
        async def stream_output(self, s):
            streamed.append(s)

    agent.llm = SimpleNamespace(astream=astream)
    agent.hitl_manager = _Hitl()
    agent._streamed = streamed
    return agent


def test_stream_plain_fallback_accumulates_content_and_usage():
    chunks = [
        SimpleNamespace(content="Hel", usage_metadata=None),
        SimpleNamespace(content="lo", usage_metadata={"total_tokens": 5}),
    ]
    agent = _agent(chunks)
    resp = asyncio.run(agent._stream_plain_fallback(["m"], 5.0))
    assert resp.content == "Hello"
    assert resp.usage_metadata == {"total_tokens": 5}
    assert agent._streamed == ["Hel", "lo"]


def test_stream_plain_fallback_handles_no_usage_metadata():
    chunks = [SimpleNamespace(content="hi", usage_metadata=None)]
    agent = _agent(chunks)
    resp = asyncio.run(agent._stream_plain_fallback(["m"], 5.0))
    assert resp.content == "hi"
    assert resp.usage_metadata is None
