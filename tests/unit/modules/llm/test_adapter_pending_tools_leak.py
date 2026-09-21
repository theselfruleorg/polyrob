"""A tool-less aux call must never inherit the previous step's toolset.

Observed on prod 2026-09-20 19:55Z: the owner-chat session compacted its
history and the compaction prompt (ONE HumanMessage, no tools) went out with
the step's 176 tool schemas attached — `[TOOLS_FIX] Retrieved 176 tools from
_pending_tools fallback` — so the summarisation call paid ~54k tokens of tool
JSON, was invited to call tools mid-summary, and took 225 s while the owner
waited for a reply.

Cause: `ainvoke`/`astream` store the caller's tools in `_pending_tools` as a
fallback for the retry paths that re-call the adapter WITHOUT kwargs, but the
main path (tools present in kwargs) never consumed the fallback, so it stayed
armed for the next tool-less call on the same adapter — compaction, reflection,
the output judge.

Rule: an explicit `tools` key in kwargs is authoritative — `tools=None` /
`tools=[]` means "no tools", and the fallback fires ONLY when the key is absent
(the retry paths, which is what it was built for).
"""
import pytest
from unittest.mock import AsyncMock

from modules.llm.adapters import OpenAIAdapter
from modules.llm.messages import HumanMessage


class _FakeClient:
    def __init__(self):
        self.model_type = "gpt-x"
        self.last_response = None
        self.generate_response = AsyncMock(return_value="plain")
        self.generate_agent_response = AsyncMock(return_value=("tooled", [], {}))


_TOOLS = [{"type": "function", "function": {"name": f"t{i}", "parameters": {}}} for i in range(3)]


def _adapter():
    return OpenAIAdapter(client=_FakeClient(), model_name="gpt-x")


@pytest.mark.asyncio
async def test_explicit_no_tools_after_a_tooled_step_sends_no_tools():
    a = _adapter()
    await a.ainvoke([HumanMessage(content="step")], tools=_TOOLS)
    assert a._client.generate_agent_response.await_count == 1

    # the compaction shape: one message, tools explicitly None
    await a.ainvoke([HumanMessage(content="summarise")], tools=None)
    assert a._client.generate_agent_response.await_count == 1, "stale tools leaked into a tool-less call"
    assert a._client.generate_response.await_count == 1


@pytest.mark.asyncio
async def test_explicit_empty_tools_is_also_authoritative():
    a = _adapter()
    await a.ainvoke([HumanMessage(content="step")], tools=_TOOLS)
    await a.ainvoke([HumanMessage(content="summarise")], tools=[])
    assert a._client.generate_agent_response.await_count == 1
    assert a._client.generate_response.await_count == 1


@pytest.mark.asyncio
async def test_absent_key_still_falls_back_for_the_retry_paths_after_a_failure():
    """The retry paths re-call the adapter with no kwargs at all after the
    tooled call RAISED (the 16:50Z / 18:21Z Connection-error recoveries); they
    must keep getting the step's tools — the one job the fallback exists for."""
    a = _adapter()
    a._client.generate_agent_response.side_effect = [RuntimeError("Connection error"), ("tooled", [], {})]
    with pytest.raises(Exception):
        await a.ainvoke([HumanMessage(content="step")], tools=_TOOLS)
    await a.ainvoke([HumanMessage(content="retry")])
    assert a._client.generate_agent_response.await_count == 2
    _, kwargs = a._client.generate_agent_response.call_args
    assert kwargs["tools"] == _TOOLS


@pytest.mark.asyncio
async def test_fallback_is_consumed_once():
    a = _adapter()
    a._client.generate_agent_response.side_effect = [RuntimeError("Connection error"), ("tooled", [], {})]
    with pytest.raises(Exception):
        await a.ainvoke([HumanMessage(content="step")], tools=_TOOLS)
    await a.ainvoke([HumanMessage(content="retry")])
    await a.ainvoke([HumanMessage(content="later, no kwargs")])
    # the second key-less call has nothing left to inherit
    assert a._client.generate_agent_response.await_count == 2
    assert a._client.generate_response.await_count == 1


@pytest.mark.asyncio
async def test_successful_tooled_call_disarms_the_fallback():
    """A step that SUCCEEDED needs no retry, so a later key-less aux call (a
    compactor that forgot `tools=None`) must not inherit its toolset either."""
    a = _adapter()
    await a.ainvoke([HumanMessage(content="step")], tools=_TOOLS)
    await a.ainvoke([HumanMessage(content="summarise, no kwargs")])
    assert a._client.generate_agent_response.await_count == 1
    assert a._client.generate_response.await_count == 1
