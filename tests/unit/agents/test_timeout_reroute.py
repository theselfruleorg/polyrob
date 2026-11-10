"""057 WS-B: consecutive timeouts are a routing fact, and three is a stop."""
import asyncio
import types

import pytest

from agents.task.agent.core.call_quality import scope_step_call
from agents.task.agent.core.timeout_reroute import (
    REROUTE_AFTER, SORT_ATTR, STOP_AFTER, maybe_reroute, note_success, note_timeout,
    reroute_enabled, should_stop_retrying,
)
from modules.llm.openrouter_routing import (
    apply_provider_routing, resolve_provider_sort, set_provider_sort,
)


class _Agent:
    session_id = "s1"

    def __init__(self):
        self.message_manager = types.SimpleNamespace()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT_REROUTE", raising=False)
    monkeypatch.delenv("OPENROUTER_PROVIDER_SORT", raising=False)
    set_provider_sort(None)
    yield
    set_provider_sort(None)


def test_off_by_default():
    assert reroute_enabled() is False
    assert should_stop_retrying(99) is False


def test_the_streak_counts_and_resets():
    a = _Agent()
    assert note_timeout(a) == 1
    assert note_timeout(a) == 2
    note_success(a)
    assert note_timeout(a) == 1, "a completed call resets the streak"


def test_no_reroute_before_the_threshold(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_REROUTE", "true")
    a = _Agent()
    assert maybe_reroute(a, REROUTE_AFTER - 1) is False
    assert getattr(a, SORT_ATTR, None) is None


def test_reroute_is_per_session_not_process_wide(monkeypatch):
    """S3: two timeouts in one session moved all sessions on the client to latency."""
    monkeypatch.setenv("LLM_TIMEOUT_REROUTE", "true")
    a, b = _Agent(), _Agent()
    assert maybe_reroute(a, REROUTE_AFTER) is True

    async def _step(agent):
        scope_step_call(agent)
        await asyncio.sleep(0)
        return resolve_provider_sort()

    async def _run():
        return await asyncio.gather(_step(a), _step(b))

    assert asyncio.run(_run()) == ["latency", None], "another session is untouched"
    import os
    assert "OPENROUTER_PROVIDER_SORT" not in os.environ


def test_reroute_is_idempotent(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_REROUTE", "true")
    a = _Agent()
    assert maybe_reroute(a, REROUTE_AFTER) is True
    assert maybe_reroute(a, REROUTE_AFTER + 1) is True
    assert getattr(a, SORT_ATTR) == "latency"


def test_the_sort_reaches_the_request_body(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_REROUTE", "true")
    a = _Agent()
    maybe_reroute(a, REROUTE_AFTER)
    scope_step_call(a)
    params = {}
    apply_provider_routing(params)
    assert params["extra_body"]["provider"]["sort"] == "latency"


def test_a_scoped_sort_beats_the_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "price")
    assert resolve_provider_sort() == "price"
    set_provider_sort("latency")
    assert resolve_provider_sort() == "latency"
    set_provider_sort(None)
    assert resolve_provider_sort() == "price", "clearing falls back to the env"


def test_an_unknown_sort_is_refused():
    assert set_provider_sort("cheapest") is False
    assert resolve_provider_sort() is None


def test_the_third_timeout_stops_the_retry(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_REROUTE", "true")
    assert should_stop_retrying(STOP_AFTER - 1) is False
    assert should_stop_retrying(STOP_AFTER) is True
