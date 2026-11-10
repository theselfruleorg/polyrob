"""One call's inputs end with its block, by token, on every Python.

``asyncio.wait_for`` runs its coroutine in the caller's task on 3.12, so the end of a
task cannot end a value. These tests await directly: no task ends here on any Python.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.llm.billing_guard import inference_sdk
from modules.llm.cache_hints import apply_cache_ttl, cache_ttl_for
from modules.llm.call_scope import (anchored_input_tokens, begin_call, call_scope, call_timeout_param,
                                    scoped_foundation_len, scoped_max_retries, session_tag, set_foundation_len,
                                    set_session_tag, set_step_context)
from modules.llm.openrouter_routing import scoped_provider_sort, set_provider_sort


def _defaults():
    return (call_timeout_param(), anchored_input_tokens(), scoped_max_retries(), scoped_foundation_len(),
            session_tag(), begin_call().context, cache_ttl_for(), scoped_provider_sort())


_DEFAULTS = ({}, None, None, None, None, {}, None, None)


def _write_each_value():
    set_foundation_len(3)
    set_session_tag("session-1")
    set_step_context({"ctx_foundation": 5})
    apply_cache_ttl("1h")
    set_provider_sort("latency")


def test_each_value_ends_with_the_block():
    with call_scope(timeout=30.0, input_tokens=9000, max_retries=0):
        _write_each_value()
        assert _defaults() == ({"timeout": 30.0}, 9000, 0, 3, session_tag(),
                               {"ctx_foundation": 5}, "1h", "latency")
    assert _defaults() == _DEFAULTS


def test_each_value_ends_when_the_call_raises():
    try:
        with call_scope(timeout=30.0, input_tokens=9000):
            _write_each_value()
            raise ValueError("the call failed")
    except ValueError:
        pass
    assert _defaults() == _DEFAULTS


def test_a_direct_await_in_the_block_leaves_nothing_in_the_task():
    async def step():
        with call_scope(timeout=30.0, input_tokens=9000):
            _write_each_value()
            await asyncio.sleep(0)
        return _defaults()
    assert asyncio.run(step()) == _DEFAULTS


def test_a_wait_for_in_the_block_reads_the_values_and_leaves_nothing():
    async def read():
        return call_timeout_param(), anchored_input_tokens(), scoped_foundation_len()

    async def step():
        with call_scope(timeout=30.0, input_tokens=9000):
            set_foundation_len(3)
            seen = await asyncio.wait_for(read(), timeout=5)
        return seen, _defaults()
    seen, after = asyncio.run(step())
    assert seen == ({"timeout": 30.0}, 9000, 3)
    assert after == _DEFAULTS


def test_a_nested_aux_call_does_not_read_the_step_anchor():
    with call_scope(timeout=30.0, input_tokens=9000):
        set_foundation_len(3)
        with call_scope(timeout=5.0, max_retries=0):
            assert anchored_input_tokens() is None
            assert call_timeout_param() == {"timeout": 5.0}
            assert scoped_foundation_len() == 3
        assert anchored_input_tokens() == 9000
        assert call_timeout_param() == {"timeout": 30.0}
        assert scoped_max_retries() is None


def test_the_sdk_reads_the_retry_count_of_the_block():
    client = SimpleNamespace(_client=MagicMock())
    assert inference_sdk(client) is client._client
    with call_scope(max_retries=0):
        assert inference_sdk(client) is client._client.with_options.return_value
    client._client.with_options.assert_called_once_with(max_retries=0)
