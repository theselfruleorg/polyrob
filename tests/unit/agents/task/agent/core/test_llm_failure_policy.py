"""Proposal 106 W1: what a failed call does on the agent side (R2, R3, D4, R12)."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from agents.task.agent.service import Agent
from agents.task.runtime.stop_reason import StopReason
from modules.llm.llm_client import translate_llm_error
from core.exceptions import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMPermanentError,
    LLMRateLimitError,
)

_REQUEST = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")


def _status_error(cls, status, message):
    return cls(message, response=httpx.Response(status, request=_REQUEST), body=None)


def _handler_agent():
    a = object.__new__(Agent)
    a.logger = logging.getLogger("t.policy")
    a.max_failures = 5
    a.retry_delay = 0
    st = MagicMock()
    st.consecutive_failures = 0
    st.track_llm_error = MagicMock(return_value=False)
    st.llm_providers_failed = []
    a.state = st
    a.message_manager = MagicMock()
    a.model_name = "deepseek/deepseek-v4-flash"
    a.telemetry_manager = MagicMock()
    a.controller = MagicMock()
    a.llm = MagicMock()
    a.llm_provider = "openrouter"
    a.orchestrator = MagicMock(session_id="s", container=None, user_id=None)
    a._attempt_llm_fallback_in_handler = AsyncMock(return_value=False)
    a._recover_from_error = AsyncMock()
    a._current_llm_provider = lambda m: "openrouter"
    return a


def _runner_agent(side_effect, fallback=None):
    a = _handler_agent()
    a._cancelled = False
    a.controller.get_action_names = MagicMock(return_value=[])
    a.use_vision = False
    a.message_manager.calculate_llm_timeout = MagicMock(return_value=5.0)
    a._get_next_action_internal = AsyncMock(side_effect=side_effect)
    a._get_fallback_llm = AsyncMock(return_value=fallback)
    a._trip_sentinel_if_credit_death = AsyncMock()
    a._emit_provider_failure_telemetry = MagicMock()
    a.adopt_active_llm = MagicMock()
    a._refresh_after_model_change = MagicMock()
    a._reprovision_aux_models = MagicMock()
    return a


_SERVER_ERROR = _status_error(openai.InternalServerError, 503, "no capacity")


@pytest.mark.parametrize("error,expected", [
    (_status_error(openai.InternalServerError, 503, "no capacity"), LLMConnectionError),
    (_status_error(openai.RateLimitError, 429, "slow down"), LLMRateLimitError),
    (_status_error(openai.APIStatusError, 402, "requires more credits"), LLMPermanentError),
])
def test_no_fallback_re_raises_the_typed_error(error, expected):
    typed = translate_llm_error(error, "OpenRouter")
    agent = _runner_agent(typed)
    with pytest.raises(expected) as caught:
        asyncio.run(agent.get_next_action([]))
    assert caught.value is typed
    if expected is LLMPermanentError:
        agent._get_fallback_llm.assert_awaited_once()
    else:
        agent._get_fallback_llm.assert_not_awaited()  # the step handler backs off first


def test_a_failed_fallback_restores_and_raises_the_first_error():
    typed = translate_llm_error(_status_error(openai.AuthenticationError, 401, "bad key"), "OpenRouter")
    fallback_error = LLMPermanentError("Error code: 402 - requires more credits")
    fallback = MagicMock(model_name="z-ai/glm-5.2")
    agent = _runner_agent([typed, fallback_error], fallback=fallback)
    with pytest.raises(LLMAuthenticationError) as caught:
        asyncio.run(agent.get_next_action([]))
    assert caught.value is typed
    assert caught.value.__suppress_context__ is True, "the fallback's 402 is not this provider's"
    assert agent._refresh_after_model_change.call_args.args[0] == "restore"


def test_a_transient_error_retries_the_same_model_first():
    """Cause 6: no ladder walk from the runner on a transient error; the step handler
    backs off on the same model first."""
    typed = translate_llm_error(_status_error(openai.RateLimitError, 429, "slow down"), "DeepSeek")
    fallback = MagicMock(model_name="z-ai/glm-5.2")
    agent = _runner_agent(typed, fallback=fallback)
    with pytest.raises(LLMRateLimitError) as caught:
        asyncio.run(agent.get_next_action([]))
    assert caught.value is typed
    agent._get_fallback_llm.assert_not_awaited()
    agent.adopt_active_llm.assert_not_called()


def test_a_5xx_with_no_fallback_backs_off_and_does_not_halt(monkeypatch):
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    agent = _handler_agent()
    agent.retry_delay = 2
    typed = translate_llm_error(_SERVER_ERROR, "OpenRouter")
    result = asyncio.run(agent._handle_step_error(typed))
    agent.state.request_stop.assert_not_called()
    assert agent.state.consecutive_failures == 1
    assert slept and slept[0] > 0
    assert result and result[0].error


def test_a_permanent_error_halts():
    agent = _handler_agent()
    typed = translate_llm_error(
        _status_error(openai.NotFoundError, 404, "does not exist"), "Anthropic",
        model="claude-retired-1")
    result = asyncio.run(agent._handle_step_error(typed))
    agent.state.request_stop.assert_called_once_with(StopReason.ERROR)
    assert "claude-retired-1" in result[0].error


def test_a_timeout_retries_once_with_the_same_context(monkeypatch):
    """No "reduced context" claim: the one retry sends the same rows."""
    import agents.task.agent.core.output_truncation as ot

    async def _passthrough(agent, msgs, timeout, result):
        return result
    monkeypatch.setattr(ot, "handle_truncated_output", _passthrough)
    rows = [object()] * 8
    agent = _runner_agent([asyncio.TimeoutError(), "SECOND"])
    agent.tool_call_tracker = None
    assert asyncio.run(agent.get_next_action(rows)) == "SECOND"
    assert [c.args[0] for c in agent._get_next_action_internal.await_args_list] == [rows, rows]


def test_a_failed_retry_after_a_timeout_requeues_the_one_shot_messages(monkeypatch):
    """No model call answered them: a correspondent reply or a recall note must not
    be committed with the recovery step."""
    agent = _runner_agent([asyncio.TimeoutError(), RuntimeError("upstream reset")])
    agent.tool_call_tracker = None
    agent._timeout_recovery_output = MagicMock(return_value="RECOVERY")
    assert asyncio.run(agent.get_next_action([object()])) == "RECOVERY"
    agent.message_manager.restore_ephemeral_on_failure.assert_called_once()


def test_a_refusal_on_the_retry_after_a_timeout_is_not_called_slow():
    from core.exceptions import LLMRefusalError
    refusal = LLMRefusalError("declined", category="cyber")
    agent = _runner_agent([asyncio.TimeoutError(), refusal])
    agent.tool_call_tracker = None
    agent._timeout_recovery_output = MagicMock(return_value="RECOVERY")
    with pytest.raises(LLMRefusalError) as caught:
        asyncio.run(agent.get_next_action([object()]))
    assert caught.value is refusal


def test_a_refusal_never_walks_the_ladder():
    """Another provider must not answer what this model declined."""
    from core.exceptions import LLMRefusalError
    refusal = LLMRefusalError("Anthropic: model m declined this request (category: cyber)",
                              category="cyber")
    agent = _runner_agent(refusal, fallback=MagicMock(model_name="z-ai/glm-5.2"))
    with pytest.raises(LLMRefusalError):
        asyncio.run(agent.get_next_action([]))
    agent._get_fallback_llm.assert_not_awaited()
    agent.adopt_active_llm.assert_not_called()


def test_a_refusal_halts_with_the_category_not_an_api_configuration_hint():
    from core.exceptions import LLMRefusalError
    agent = _handler_agent()
    result = asyncio.run(agent._handle_step_error(
        LLMRefusalError("declined", category="cyber")))
    agent.state.request_stop.assert_called_once_with(StopReason.ERROR)
    agent._attempt_llm_fallback_in_handler.assert_not_awaited()
    assert "declined this request (category: cyber)" in result[0].error
    assert "API configuration" not in result[0].error
    from core.exceptions import REFUSAL_HALT_PREFIX
    assert result[0].error.startswith(REFUSAL_HALT_PREFIX), "other seats find it by the prefix"
    filtered = asyncio.run(_handler_agent()._handle_step_error(
        LLMRefusalError("filtered", kind="filtered")))
    assert "safety filter stopped" in filtered[0].error


def test_a_context_overflow_reaches_the_emergency_prune():
    agent = _handler_agent()
    typed = translate_llm_error(_status_error(
        openai.BadRequestError, 400, "maximum context length is 128000, requested 130402 tokens"))
    assert isinstance(typed, LLMContextLengthError)
    asyncio.run(agent._handle_step_error(typed))
    agent.message_manager.emergency_context_prune.assert_called_once()
    agent.state.request_stop.assert_not_called()


def test_a_transient_that_stays_reports_through_the_circuit_breaker():
    from core.exceptions import LLMProviderExhaustedError
    agent = _handler_agent()
    agent.state.track_llm_error = MagicMock(return_value=True)
    with pytest.raises(LLMProviderExhaustedError, match="exhausted after LLMConnectionError"):
        asyncio.run(agent._handle_step_error(translate_llm_error(_SERVER_ERROR)))
    agent.state.request_stop.assert_called_once_with(StopReason.ERROR)


# R2: a failed native call makes no second paid call.

class _MM:
    def __init__(self):
        self.calls = []

    def get_estimated_context_usage(self):
        return 0.1

    def get_messages(self):
        return []

    def check_token_safety(self, *a, **k):
        return {"safe": True, "usage_percent": 0.0, "current_tokens": 0, "max_limit": 1000}

    def get_messages_for_llm(self):
        return []

    def get_token_count(self):
        return 0

    def get_actual_token_count(self):
        return 4242

    def calculate_llm_timeout(self, tool_count=0, use_vision=False):
        return 30

    def get_llm_parameters(self):
        return {}

    def push_ephemeral_message(self, message):
        pass

    def set_tool_schema_tokens(self, tokens):
        pass


def _native_agent(ainvoke):
    a = object.__new__(Agent)
    a.logger = logging.getLogger("t.native")
    controller = MagicMock()
    controller.supports_native_tools = MagicMock(return_value=True)
    controller.get_all_actions_for_provider = MagicMock(
        return_value=[{"type": "function", "function": {"name": "done", "parameters": {}}}])
    controller.get_action_names = MagicMock(return_value=["done"])
    controller.get_tool_schema_token_estimate = MagicMock(return_value=10)
    a.controller = controller
    a.tool_call_tracker = None
    a.message_manager = _MM()
    a.model_name = "z-ai/glm-5.2"
    a.chat_model_library = "OpenRouterAdapter"
    a.use_native_tools = True
    a.use_vision = False
    a.provider_name = "fake-test-provider"
    a.state = SimpleNamespace(n_steps=2)
    a.llm = SimpleNamespace(ainvoke=ainvoke)
    a.max_actions_per_step = 5
    a.telemetry_manager = MagicMock()
    a.hitl_manager = MagicMock()
    a.hitl_manager.has_streaming_callbacks = MagicMock(return_value=False)
    a.agent_id = "a"
    return a


@pytest.mark.parametrize("error", [
    LLMConnectionError("no capacity"),
    LLMPermanentError("requires more credits"),
    LLMContextLengthError("context length"),
])
def test_a_failed_native_call_makes_no_second_call(error):
    ainvoke = AsyncMock(side_effect=error)
    agent = _native_agent(ainvoke)
    with pytest.raises(type(error)):
        asyncio.run(agent._get_next_action_internal([]))
    assert ainvoke.await_count == 1
    assert "tools" in ainvoke.call_args.kwargs and ainvoke.call_args.kwargs["tools"]


def test_a_native_timeout_propagates_with_no_second_call():
    ainvoke = AsyncMock(side_effect=asyncio.TimeoutError())
    agent = _native_agent(ainvoke)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(agent._get_next_action_internal([]))
    assert ainvoke.await_count == 1


def test_the_plain_call_says_no_tools():
    """No `tools` key would inherit the adapter's armed _pending_tools."""
    ainvoke = AsyncMock(side_effect=LLMPermanentError("x"))
    agent = _native_agent(ainvoke)
    agent.controller = None
    agent.use_native_tools = False
    with pytest.raises(LLMPermanentError):
        asyncio.run(agent._get_next_action_internal([]))
    assert ainvoke.call_args.kwargs == {"tools": None}


def test_the_native_call_scopes_the_budget_and_the_anchor():
    from modules.llm.call_scope import _call_scope
    seen = {}

    async def ainvoke(messages, tools=None):
        seen["scope"] = _call_scope.get()
        raise LLMPermanentError("stop here")

    agent = _native_agent(ainvoke)
    with pytest.raises(LLMPermanentError):
        asyncio.run(agent._get_next_action_internal([]))
    assert seen["scope"].timeout == 30
    assert seen["scope"].input_tokens == 4242


def test_the_call_sends_its_extra_rows_and_a_blind_model_gets_no_image():
    """The request is rebuilt from the manager: a note passed by the caller (the truncation
    retry) and the image strip for a model with no vision must reach THAT request."""
    from modules.llm.messages import HumanMessage
    seen = {}

    async def ainvoke(messages, tools=None):
        seen["messages"] = list(messages)
        raise LLMPermanentError("stop here")

    agent = _native_agent(ainvoke)
    image = HumanMessage(content=[{"type": "text", "text": "look"},
                                  {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])
    agent.message_manager.get_messages_for_llm = lambda: [image]
    agent._check_vision_support = lambda model: False
    with pytest.raises(LLMPermanentError):
        asyncio.run(agent._get_next_action_internal(
            [], extra_messages=[HumanMessage(content="CUT OFF note")]))
    sent = seen["messages"]
    assert sent[-1].content == "CUT OFF note"
    assert not any(isinstance(m.content, list) for m in sent), "the image left the request"
    assert "vision disabled" in sent[0].content


@pytest.mark.parametrize("native", [True, False])
def test_the_task_reads_defaults_after_the_step_call(native):
    """A direct await, so no task ends on any Python: only the scope can end the values."""
    from modules.llm.cache_hints import cache_ttl_for
    from modules.llm.call_scope import scoped_foundation_len as foundation_len_for
    from modules.llm.call_scope import (anchored_input_tokens, begin_call, call_timeout_param,
                                        session_tag)
    from modules.llm.openrouter_routing import scoped_provider_sort
    seen = {}

    async def ainvoke(messages, tools=None):
        seen.update(tag=session_tag(), timeout=call_timeout_param())
        raise LLMPermanentError("stop here")

    agent = _native_agent(ainvoke)
    agent.orchestrator = SimpleNamespace(session_id="s-1")
    agent.message_manager._last_foundation_len = 3
    if not native:
        agent.controller = None
        agent.use_native_tools = False

    async def step():
        with pytest.raises(LLMPermanentError):
            await agent._get_next_action_internal([])
        return (call_timeout_param(), anchored_input_tokens(), foundation_len_for(), session_tag(),
                begin_call().context, cache_ttl_for(), scoped_provider_sort())

    assert asyncio.run(step()) == ({}, None, None, None, {}, None, None)
    assert seen["tag"] and seen["timeout"] == {"timeout": 30}


# R12: only a missing client moves the request.

def _lite_agent(manager):
    from agents.task_agent_lite import TaskAgent
    lite = object.__new__(TaskAgent)
    lite.container = SimpleNamespace(get_service=lambda name: manager)
    return lite


@pytest.fixture
def lite_request():
    return {"provider": "openrouter", "model": "z-ai/glm-5.2"}


def test_lite_billing_refusal_does_not_move_the_request(lite_request):
    manager = SimpleNamespace(
        clients={"openrouter_client": object()},
        get_chat_model=AsyncMock(side_effect=ValueError(
            "Billed requests require an explicitly priced model")),
        get_fallback_chat_model=AsyncMock(),
    )
    lite = _lite_agent(manager)
    with pytest.raises(ValueError, match="explicitly priced"):
        asyncio.run(lite._get_llm_for_request(lite_request))
    manager.get_fallback_chat_model.assert_not_awaited()


def test_lite_missing_client_moves_the_request(lite_request):
    fallback = MagicMock(model_name="deepseek-flash")
    manager = SimpleNamespace(
        clients={"deepseek_client": object()},
        get_chat_model=AsyncMock(side_effect=ValueError("not available")),
        get_fallback_chat_model=AsyncMock(return_value=fallback),
    )
    lite = _lite_agent(manager)
    assert asyncio.run(lite._get_llm_for_request(lite_request)) is fallback


def test_the_step_call_names_no_undefined_stream_text_and_keeps_no_request_log():
    """The streaming telemetry read an undefined name (a NameError swallowed into a debug
    line), and every call appended to a session_data list nobody read."""
    import inspect
    from agents.task.agent.core import construction_helpers, next_action_internal
    code = Agent._get_next_action_internal
    assert "full_content" not in code.__code__.co_names
    assert "llm_requests" not in inspect.getsource(next_action_internal)
    assert "llm_requests" not in inspect.getsource(construction_helpers)
