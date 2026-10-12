"""D3 (prod 2026-10-09): z.ai GLM answered HTTP 429 type=rate_limit_error with
code 1113 "Insufficient balance or no resource package". It was wrapped as a
generic error and retried as a rate limit. A credit refusal is now ONE
LLMPermanentError that names the provider and says NO BALANCE."""
import asyncio
import logging

import pytest

from core.exceptions import LLMPermanentError
from modules.llm.credit_errors import provider_credit_error

ZAI_1113 = ("Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error', "
            "'code': '1113', 'message': '[1113][Insufficient balance or no resource "
            "package. Please recharge.][2026100915015121e7ff0801f04d4c]'}}")


def test_1113_becomes_a_named_no_balance_permanent_error():
    err = provider_credit_error("z.ai GLM Coding Plan", RuntimeError(ZAI_1113))
    assert isinstance(err, LLMPermanentError)
    text = str(err)
    assert "z.ai GLM Coding Plan: NO BALANCE" in text
    assert "provider code 1113" in text
    assert "not a rate limit" in text


def test_a_real_rate_limit_is_left_alone():
    assert provider_credit_error("z.ai", RuntimeError(
        "Error code: 429 - rate_limit_error: too many requests")) is None


def test_an_already_named_error_passes_through_unchanged():
    first = provider_credit_error("z.ai", RuntimeError(ZAI_1113))
    assert provider_credit_error("z.ai", first) is first


def _bare_client():
    pytest.importorskip("anthropic")
    from modules.llm.anthropic_client import AnthropicClient
    c = object.__new__(AnthropicClient)
    c.logger = logging.getLogger("t")
    c._PROVIDER_LABEL = "z.ai GLM Coding Plan"
    return c


def test_tool_path_raises_the_permanent_error_not_a_generic_llm_error():
    c = _bare_client()

    async def _boom(**kw):
        raise RuntimeError(ZAI_1113)
    c._generate_with_tools = _boom
    with pytest.raises(LLMPermanentError, match="NO BALANCE"):
        asyncio.run(c.generate_agent_response([{"role": "user", "content": "x"}], tools=[]))


def test_plain_path_keeps_the_permanent_type_through_the_outer_wrapper():
    c = _bare_client()

    async def _boom(**kw):
        raise provider_credit_error(c._PROVIDER_LABEL, RuntimeError(ZAI_1113))
    c._generate = _boom
    c._format_messages = lambda m: m
    with pytest.raises(LLMPermanentError, match="NO BALANCE"):
        asyncio.run(c.generate_response([{"role": "user", "content": "x"}]))


def _sdk_httpx(anthropic):
    """The httpx package the installed SDK is built on: ``httpx`` (anthropic
    < 1.x) or ``httpx2`` (the 1.x line in requirements.lock). The SDK refuses a
    transport from the other one, and the product hook is duck-typed over both."""
    import importlib
    for base in anthropic.DefaultAsyncHttpxClient.__mro__:
        root = base.__module__.split(".")[0]
        if root in ("httpx", "httpx2"):
            return importlib.import_module(root)
    return importlib.import_module("httpx")


def _count_calls(body: dict, *, hook: bool) -> int:
    """HTTP calls the Anthropic SDK makes for one 429 (max_retries default 2)."""
    anthropic = pytest.importorskip("anthropic")
    httpx = _sdk_httpx(anthropic)
    from modules.llm.credit_errors import no_retry_on_credit_refusal

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json=body, headers={"retry-after-ms": "1"})

    hooks = {"response": [no_retry_on_credit_refusal]} if hook else {}
    client = anthropic.AsyncAnthropic(
        api_key="k", base_url="http://zai.test",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx.MockTransport(handler), event_hooks=hooks))

    async def go():
        with pytest.raises(anthropic.RateLimitError):
            await client.messages.create(model="glm", max_tokens=1,
                                         messages=[{"role": "user", "content": "hi"}])
    asyncio.run(go())
    return len(calls)


_ZAI_BODY = {"type": "error", "error": {"type": "rate_limit_error", "code": "1113",
             "message": "[1113][Insufficient balance or no resource package. Please recharge.]"}}


def test_a_credit_refusal_is_one_http_call_not_three():
    assert _count_calls(_ZAI_BODY, hook=False) == 3
    assert _count_calls(_ZAI_BODY, hook=True) == 1


def test_a_real_rate_limit_keeps_the_sdk_retries():
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "too many requests"}}
    assert _count_calls(body, hook=True) == 3


def test_the_compat_client_installs_the_hook():
    pytest.importorskip("anthropic")
    from modules.llm.compat_anthropic import AnthropicCompatClient
    from modules.llm.credit_errors import no_retry_on_credit_refusal
    from modules.llm.provider_spec import get_spec

    c = object.__new__(AnthropicCompatClient)
    c._spec = get_spec("zai-coding")
    c.api_key = "k"
    c.logger = logging.getLogger("t")
    asyncio.run(c._setup_client())
    assert no_retry_on_credit_refusal in c._client._client.event_hooks["response"]


def test_cleanup_closes_the_sdk_client():
    """The httpx pool is closed, not dropped (review 2026-10-11)."""
    c = _bare_client()
    closed = []

    class _SDK:
        async def close(self):
            closed.append(True)
    c._client = _SDK()
    c.logger = logging.getLogger("t")
    asyncio.run(c._cleanup_client())
    assert closed and c._client is None
