"""Task 5c fix pass 2 (money-correctness): the provider's own response id must
be stamped onto the PER-CALL AIMessage object at the point `_agenerate`
captures the raw provider response -- for BOTH the native tool-calling path
and the plain (no-tools) path -- so `extract_stable_request_id`
(agents/task/agent/core/aux_metering.py) can read a stable, per-call billing
dedup key without racing a concurrent call that shares the same underlying
LLM client object (parallel sub-agent delegation shares `parent_agent.llm`
verbatim -- see SubAgentManager.run_subtask).
"""
import pytest
from unittest.mock import AsyncMock

from modules.llm.adapters import OpenAIAdapter
from modules.llm.messages import HumanMessage


class _FakeClient:
    """Mimics a concrete LLM client: `last_response` is set on every call
    (mirroring AnthropicClient/OpenAIClient/etc.), and both the native
    tool-calling (`generate_agent_response`) and plain (`generate_response`)
    entry points are available."""

    def __init__(self, raw_response_id="msg_x"):
        self.model_type = "gpt-x"
        self.last_response = None
        self._raw_response_id = raw_response_id

        async def _generate_response(**kwargs):
            # Mirrors a real client: stash the raw SDK response before
            # returning the flat content string.
            self.last_response = _RawResponse(self._raw_response_id)
            return "hi there"

        async def _generate_agent_response(**kwargs):
            self.last_response = _RawResponse(self._raw_response_id)
            # Canonical 3-tuple contract: (content, tool_calls, usage)
            return ("using a tool", [{"id": "call_1", "name": "noop", "args": {}}], {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            })

        self.generate_response = AsyncMock(side_effect=_generate_response)
        self.generate_agent_response = AsyncMock(side_effect=_generate_agent_response)


class _RawResponse:
    """Mimics a raw SDK response object carrying a provider completion id
    (Anthropic `msg_...`, OpenAI `chatcmpl-...`)."""

    def __init__(self, id_):
        self.id = id_


def _adapter(**kw):
    return OpenAIAdapter(client=_FakeClient(**kw), model_name="gpt-x")


@pytest.mark.asyncio
async def test_plain_path_stamps_provider_response_id_onto_ai_message():
    a = _adapter(raw_response_id="msg_plain_x")
    result = await a._agenerate([HumanMessage(content="hello")])
    ai_message = result.generations[0].message
    assert ai_message._polyrob_provider_response_id == "msg_plain_x"


@pytest.mark.asyncio
async def test_tool_calling_path_stamps_provider_response_id_onto_ai_message():
    a = _adapter(raw_response_id="msg_tools_x")
    result = await a._agenerate(
        [HumanMessage(content="hello")],
        tools=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
    )
    ai_message = result.generations[0].message
    assert ai_message.tool_calls  # sanity: really took the tool-calling branch
    assert ai_message._polyrob_provider_response_id == "msg_tools_x"


@pytest.mark.asyncio
async def test_ainvoke_returns_the_same_stamped_object():
    """The public entry point callers actually use (`llm.ainvoke(...)`) must
    return the SAME per-call object carrying the stamp -- not a copy that
    drops it -- since next_action_internal.py bills exactly what `ainvoke`
    returns."""
    a = _adapter(raw_response_id="msg_ainvoke_x")
    response = await a.ainvoke([HumanMessage(content="hello")])
    assert response._polyrob_provider_response_id == "msg_ainvoke_x"


@pytest.mark.asyncio
async def test_no_id_on_raw_response_leaves_attribute_absent():
    """Not every provider's raw response carries an id (e.g. Gemini) --
    the attribute must simply be absent (never fabricated), so
    extract_stable_request_id honestly falls through to its uuid fallback."""
    class _NoIdClient:
        def __init__(self):
            self.model_type = "gpt-x"
            self.last_response = None

            async def _generate_response(**kwargs):
                self.last_response = object()  # no `.id` at all
                return "hi there"

            self.generate_response = AsyncMock(side_effect=_generate_response)

    a = OpenAIAdapter(client=_NoIdClient(), model_name="gpt-x")
    result = await a._agenerate([HumanMessage(content="hello")])
    ai_message = result.generations[0].message
    assert ai_message._polyrob_provider_response_id is None


@pytest.mark.asyncio
async def test_two_concurrent_calls_on_one_adapter_each_keep_their_own_id():
    """The regression scenario end-to-end at the adapter layer: two
    sequential calls on the SAME adapter/client (as two concurrent sub-agents
    sharing `parent_agent.llm` would each drive) each produce their OWN
    AIMessage carrying their OWN id, even though `last_response` (the shared
    slot) ends up pointing at whichever call ran last."""
    client = _FakeClient(raw_response_id="msg_first")
    a = OpenAIAdapter(client=client, model_name="gpt-x")

    result_first = await a._agenerate([HumanMessage(content="first")])
    msg_first = result_first.generations[0].message

    # Simulate a second, distinct completion landing on the SAME shared client
    # (mutates `last_response` out from under the first call, exactly as a
    # concurrent sub-agent's completion would).
    client._raw_response_id = "msg_second"
    result_second = await a._agenerate([HumanMessage(content="second")])
    msg_second = result_second.generations[0].message

    # The first message's stamped id must NOT have been mutated by the
    # second call overwriting the shared client's last_response.
    assert msg_first._polyrob_provider_response_id == "msg_first"
    assert msg_second._polyrob_provider_response_id == "msg_second"
