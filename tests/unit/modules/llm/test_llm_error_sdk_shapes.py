"""Proposal 106 W1: real SDK exceptions reach the recovery code typed.

Every error here is the object the SDK raises, so ``status_code`` is read as in
production. The clients and the adapter must keep the type, or context recovery, the
402 fail-fast and the provider health never run.
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import openai
import pytest
from openai.types.chat import ChatCompletion

from core.llm_limits import llm_request_timeout
from modules.llm.llm_client import translate_llm_error
from core.exceptions import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMError,
    LLMInvalidRequestError,
    LLMPermanentError,
    LLMModerationError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMResponseError,
)

_REQUEST = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")


def _openai(cls, status, message):
    return cls(message, response=httpx.Response(status, request=_REQUEST), body=None)


def _anthropic(cls, status, message):
    return cls(message, response=httpx.Response(status, request=_REQUEST), body=None)


@pytest.mark.parametrize("error,expected", [
    # Context length, whatever digits the message holds.
    (_openai(openai.BadRequestError, 400,
             "This model's maximum context length is 128000 tokens. However, you requested "
             "130402 tokens"), LLMContextLengthError),
    (_openai(openai.BadRequestError, 400,
             "maximum context length is 14290 tokens, you requested 16429 tokens"),
     LLMContextLengthError),
    (_anthropic(anthropic.BadRequestError, 400, "prompt is too long: 210401 tokens > 200000 maximum"),
     LLMContextLengthError),
    (_openai(openai.APIStatusError, 413, "payload"), LLMContextLengthError),
    # A 429 with a token count is a rate limit, and "tokens" does not make it an overflow.
    (_openai(openai.RateLimitError, 429,
             "Rate limit reached on tokens per min. Limit 30000, requested 402"), LLMRateLimitError),
    (_anthropic(anthropic.RateLimitError, 429,
                "would exceed the rate limit of 30000 input tokens per minute"), LLMRateLimitError),
    # A 429 can be a dead account.
    (_openai(openai.RateLimitError, 429,
             "You exceeded your current quota. insufficient_quota"), LLMPermanentError),
    (_anthropic(anthropic.RateLimitError, 429,
                "[1113][Insufficient balance or no resource package]"), LLMPermanentError),
    # 402: dead account, or funded and waiting for in-flight requests.
    (_openai(openai.APIStatusError, 402, "requires more credits"), LLMPermanentError),
    (_openai(openai.APIStatusError, 402,
             "exceed your available credits given your current in-flight requests. "
             "Retry after in-flight requests settle"), LLMRateLimitError),
    (_anthropic(anthropic.BadRequestError, 400, "Your credit balance is too low"), LLMPermanentError),
    # Auth.
    (_openai(openai.AuthenticationError, 401, "User not found."), LLMAuthenticationError),
    (_anthropic(anthropic.PermissionDeniedError, 403, "no access"), LLMAuthenticationError),
    # A moderation 403 is a refusal: no other provider answers it in its place.
    (_openai(openai.PermissionDeniedError, 403, "your input was flagged by moderation"),
     LLMModerationError),
    # A 404 is permanent.
    (_openai(openai.NotFoundError, 404, "The model does not exist"), LLMPermanentError),
    (_anthropic(anthropic.NotFoundError, 404, "not_found_error"), LLMPermanentError),
    # Transient.
    (_openai(openai.InternalServerError, 500, "boom"), LLMConnectionError),
    (_openai(openai.InternalServerError, 503, "no capacity, billing is fine"), LLMConnectionError),
    (_anthropic(anthropic.InternalServerError, 529, "Overloaded"), LLMConnectionError),
    (openai.APIConnectionError(request=_REQUEST), LLMConnectionError),
    (openai.APITimeoutError(request=_REQUEST), LLMConnectionError),
    (anthropic.APITimeoutError(request=_REQUEST), LLMConnectionError),
    (_openai(openai.APIStatusError, 408, "request timeout"), LLMConnectionError),
    # A plain bad request stays one.
    (_openai(openai.BadRequestError, 400, "tools[0] is malformed"), LLMInvalidRequestError),
    (_openai(openai.UnprocessableEntityError, 422, "unknown field"), LLMInvalidRequestError),
], ids=lambda v: None if isinstance(v, type) else f"{type(v).__name__}-{str(v)[:30]}")
def test_status_decides(error, expected):
    assert type(translate_llm_error(error, "ctx")) is expected


@pytest.mark.parametrize("text,expected", [
    ("however you requested 130402 tokens, maximum context length exceeded", LLMContextLengthError),
    ("used 14290 tokens of the budget", LLMError),
    ("used 429 tokens of the budget", LLMError),
    ("spent $0.402 on the call", LLMError),
    ("the author field is wrong", LLMError),
    ("moderate limit applied", LLMError),
    ("the job took too long", LLMError),
    ("req_9f402ab13c7e failed", LLMError),
    ("Error code: 402 - out", LLMPermanentError),
    ("Error code: 429 - slow down", LLMRateLimitError),
    ("Error code: 401", LLMAuthenticationError),
    ("Error code: 503", LLMConnectionError),
    ("Connection error.", LLMConnectionError),
])
def test_text_path_is_word_bounded(text, expected):
    assert type(translate_llm_error(Exception(text))) is expected


def test_a_builtin_timeout_with_no_text_is_a_connection_error():
    assert type(translate_llm_error(TimeoutError())) is LLMConnectionError


def test_a_typed_error_passes_unchanged():
    err = LLMRateLimitError("slow")
    assert translate_llm_error(err, "ctx") is err


_NO_ENDPOINTS = {"error": {"message": "No endpoints found for acme/some-model.", "code": 404}}
_NO_PROVIDERS = {"error": {"message": "No allowed providers are available for the selected model.",
                           "code": 404}}


def _openrouter_404(body):
    return openai.NotFoundError(f"Error code: 404 - {body}",
                                response=httpx.Response(404, request=_REQUEST), body=body)


@pytest.mark.parametrize("body", [_NO_ENDPOINTS, _NO_PROVIDERS])
def test_an_openrouter_route_with_no_upstream_can_fail_over(body):
    typed = translate_llm_error(_openrouter_404(body), "OpenRouter", model="acme/some-model")
    assert type(typed) is LLMConnectionError
    assert "a retry cannot help" not in str(typed)


@pytest.mark.parametrize("body", [_NO_ENDPOINTS, _NO_PROVIDERS])
def test_a_route_with_no_upstream_in_a_200_body_can_fail_over(body):
    from modules.llm.error_rules import ProviderBodyError
    assert type(translate_llm_error(ProviderBodyError(body["error"]))) is LLMConnectionError


def test_an_openrouter_model_not_found_stays_permanent():
    body = {"error": {"message": "The model `acme/gone` does not exist", "code": 404}}
    typed = translate_llm_error(_openrouter_404(body), "OpenRouter", model="acme/gone")
    assert type(typed) is LLMPermanentError
    assert "acme/gone not found (404)" in str(typed)


def test_a_404_names_the_model():
    err = translate_llm_error(
        _anthropic(anthropic.NotFoundError, 404, "not_found_error"), "Anthropic",
        model="claude-retired-1")
    assert isinstance(err, LLMPermanentError)
    assert "claude-retired-1" in str(err)
    assert "402" not in str(err)


def test_status_survives_a_wrapper():
    inner = _openai(openai.InternalServerError, 503, "boom")
    try:
        try:
            raise inner
        except Exception as e:
            raise RuntimeError("Failed to initialize client") from e
    except RuntimeError as wrapper:
        assert type(translate_llm_error(wrapper)) is LLMConnectionError


def _sdk(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _openrouter_client():
    from modules.llm.openrouter_client import OpenRouterClient
    c = object.__new__(OpenRouterClient)
    c.logger = logging.getLogger("t.openrouter")
    c.model_type = "z-ai/glm-5.2"
    c.temperature = 0.0
    c.max_tokens = 1000
    c.last_response = None
    c._initialized = True
    c._PROVIDER_LABEL = "OpenRouter"
    return c


def _openai_client():
    from modules.llm.openai_client import OpenAIClient
    c = object.__new__(OpenAIClient)
    c.logger = logging.getLogger("t.openai")
    c.model_type = "gpt-5"
    c.temperature = 0.0
    c.max_tokens = 1000
    c.last_response = None
    c._initialized = True
    return c


_SDK_ERRORS = [
    (_openai(openai.BadRequestError, 400, "maximum context length is 128000, requested 130402 tokens"),
     LLMContextLengthError),
    (_openai(openai.APIStatusError, 402, "requires more credits"), LLMPermanentError),
    (_openai(openai.RateLimitError, 429, "slow down"), LLMRateLimitError),
    (_openai(openai.InternalServerError, 500, "boom"), LLMConnectionError),
]


@pytest.mark.parametrize("error,expected", _SDK_ERRORS)
@pytest.mark.parametrize("build", [_openrouter_client, _openai_client])
def test_tool_path_keeps_the_type(build, error, expected):
    c = build()
    tool = {"type": "function", "function": {"name": "t", "parameters": {}}}
    with patch(f"{type(c).__module__}.inference_sdk", return_value=_sdk(AsyncMock(side_effect=error))):
        with pytest.raises(expected) as caught:
            asyncio.run(c.generate_agent_response(
                messages=[{"role": "user", "content": "x"}], tools=[tool]))
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("error,expected", _SDK_ERRORS)
@pytest.mark.parametrize("build", [_openrouter_client, _openai_client])
def test_plain_path_keeps_the_type(build, error, expected):
    c = build()
    with patch(f"{type(c).__module__}.inference_sdk", return_value=_sdk(AsyncMock(side_effect=error))):
        with pytest.raises(expected):
            asyncio.run(c.generate_response(messages=[{"role": "user", "content": "x"}]))


@pytest.mark.parametrize("error,expected", [
    (_anthropic(anthropic.BadRequestError, 400, "prompt is too long: 210401 tokens > 200000 maximum"),
     LLMContextLengthError),
    (_anthropic(anthropic.InternalServerError, 529, "Overloaded"), LLMConnectionError),
    (_anthropic(anthropic.NotFoundError, 404, "not_found_error"), LLMPermanentError),
])
def test_anthropic_wrap_keeps_the_type(error, expected):
    from modules.llm.anthropic_client import AnthropicClient
    c = object.__new__(AnthropicClient)
    c.logger = logging.getLogger("t.anthropic")
    c.model_type = "claude-retired-1"
    c._PROVIDER_LABEL = "Anthropic"
    c._generate_with_tools = AsyncMock(side_effect=error)
    with pytest.raises(expected):
        asyncio.run(c.generate_agent_response([{"role": "user", "content": "x"}], tools=[]))


def test_the_adapter_keeps_the_type_and_stamps_health():
    from modules.llm.adapters import OpenRouterAdapter
    from modules.llm.init_gate import ProviderInitGate, recent_call_error
    from modules.llm.messages import HumanMessage
    c = _openrouter_client()
    c._health_gate = ProviderInitGate()
    error = _openai(openai.InternalServerError, 500, "boom")
    adapter = OpenRouterAdapter(client=c, model_name="z-ai/glm-5.2")
    tool = {"type": "function", "function": {"name": "t", "parameters": {}}}
    with patch("modules.llm.openrouter_client.inference_sdk",
               return_value=_sdk(AsyncMock(side_effect=error))):
        with pytest.raises(LLMConnectionError):
            asyncio.run(adapter.ainvoke([HumanMessage(content="x")], tools=[tool]))
    assert recent_call_error(c)


# R10: an error or a filter inside an HTTP 200.

def _completion(**fields):
    return ChatCompletion.construct(id="gen-1", **fields)


def _choice(finish_reason, content="text"):
    return SimpleNamespace(finish_reason=finish_reason, error=None,
                           message=SimpleNamespace(content=content, tool_calls=None))


@pytest.mark.parametrize("build", [_openrouter_client, _openai_client])
@pytest.mark.parametrize("response,expected,named", [
    (_completion(choices=None, error={"code": 502, "message": "Upstream said no"}),
     LLMConnectionError, "Upstream said no"),
    (_completion(choices=None, error={"code": 429, "message": "slow"}), LLMRateLimitError, "slow"),
    (_completion(choices=None, error={"code": 402, "message": "requires more credits"}),
     LLMPermanentError, "credits"),
    (_completion(choices=[], usage=None), LLMResponseError, "no choices"),
    (_completion(choices=[_choice("content_filter", "")]), LLMRefusalError, "content filter"),
    (_completion(choices=[_choice("error", "")]), LLMResponseError, "finish_reason=error"),
])
def test_a_200_with_an_error_raises_typed(build, response, expected, named):
    c = build()
    tool = {"type": "function", "function": {"name": "t", "parameters": {}}}
    with patch(f"{type(c).__module__}.inference_sdk",
               return_value=_sdk(AsyncMock(return_value=response))):
        with pytest.raises(expected, match=named):
            asyncio.run(c.generate_agent_response(
                messages=[{"role": "user", "content": "x"}], tools=[tool]))
        with pytest.raises(expected, match=named):
            asyncio.run(c.generate_response(messages=[{"role": "user", "content": "x"}]))


def test_a_normal_stop_passes():
    c = _openrouter_client()
    choice = _choice("stop")
    assert c._first_choice(_completion(choices=[choice])) is choice


# R9: every SDK constructor gets the one limit.

@pytest.mark.parametrize("module,cls", [
    ("modules.llm.openrouter_client", "OpenRouterClient"),
    ("modules.llm.deepseek_client", "DeepSeekClient"),
    ("modules.llm.nvidia_client", "NvidiaClient"),
    ("modules.llm.openai_client", "OpenAIClient"),
])
def test_each_constructor_passes_the_limits(module, cls, monkeypatch):
    import importlib
    from modules.llm.llm_client import LLMClient
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT_SECONDS", raising=False)
    mod = importlib.import_module(module)
    c = object.__new__(getattr(mod, cls))
    c.logger = logging.getLogger("t.ctor")
    c.api_key = "k"
    c.organization = None
    c.base_url = None
    c.site_url = None
    c.site_name = None
    c._api_url_override = "https://provider.invalid/v1"
    c._client = None
    with patch(f"{module}.AsyncOpenAI") as sdk:
        asyncio.run(c._setup_client())
    kwargs = sdk.call_args.kwargs
    assert kwargs["timeout"] == float(llm_request_timeout())
    assert kwargs["max_retries"] == LLMClient.DEFAULT_MAX_RETRIES


def test_compat_constructor_passes_the_limits(monkeypatch):
    from modules.llm.compat_clients import OpenAICompatClient
    from modules.llm.llm_client import LLMClient
    c = object.__new__(OpenAICompatClient)
    c.logger = logging.getLogger("t.compat")
    c.api_key = "k"
    c._spec = MagicMock()
    c._spec.headers.return_value = {}
    c._profile_base_url = lambda: "https://provider.invalid/v1"
    with patch("openai.AsyncOpenAI") as sdk:
        asyncio.run(c._setup_client())
    assert sdk.call_args.kwargs["max_retries"] == LLMClient.DEFAULT_MAX_RETRIES
    assert sdk.call_args.kwargs["timeout"] == float(llm_request_timeout())


def test_the_flag_moves_the_timeout(monkeypatch):
    c = _openrouter_client()
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "45")
    assert c._sdk_limits()["timeout"] == 45.0


def test_a_step_budget_overrides_the_sdk_limit_on_the_wire():
    """The step's wait_for owns a main call: 360 s allowed must not be cut at 120 s."""
    from modules.llm.call_scope import call_scope, call_timeout_param
    c = _openrouter_client()
    create = AsyncMock(return_value=_completion(
        choices=[_choice("stop")], usage=None))
    tool = {"type": "function", "function": {"name": "t", "parameters": {}}}

    async def scoped():
        with call_scope(timeout=360.0):
            await c.generate_agent_response(messages=[{"role": "user", "content": "x"}], tools=[tool])

    with patch("modules.llm.openrouter_client.inference_sdk", return_value=_sdk(create)):
        asyncio.run(scoped())
    assert create.call_args.kwargs["timeout"] == 360.0
    assert call_timeout_param() == {}, "the scope ends with its block"


def test_a_moderation_403_is_a_filtered_refusal_and_not_stream_specific():
    from core.exceptions import LLMRefusalError
    from modules.llm.adapters import _is_stream_specific
    typed = translate_llm_error(
        _openai(openai.PermissionDeniedError, 403, "your stream input was flagged by moderation"),
        "OpenRouter")
    assert isinstance(typed, LLMRefusalError) and typed.kind == "filtered"
    assert "safety filter" in typed.owner_text()
    assert not _is_stream_specific(typed), "a batch retry would re-send the refused request"
