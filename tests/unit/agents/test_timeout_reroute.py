"""057 WS-B: consecutive timeouts are a routing fact, and three is a stop."""
import types

import pytest

from agents.task.agent.core.timeout_reroute import (
    REROUTE_AFTER, STOP_AFTER, maybe_reroute, note_success, note_timeout,
    reroute_enabled, should_stop_retrying,
)
from modules.llm.openrouter_routing import (
    apply_provider_routing, resolve_provider_sort, set_provider_sort,
)


class _Client:
    pass


class _Agent:
    def __init__(self):
        self.llm = types.SimpleNamespace(_client=_Client())


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT_REROUTE", raising=False)
    monkeypatch.delenv("OPENROUTER_PROVIDER_SORT", raising=False)


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
    assert resolve_provider_sort(a.llm._client) is None


def test_reroute_is_per_session_not_process_wide(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_REROUTE", "true")
    a, b = _Agent(), _Agent()
    assert maybe_reroute(a, REROUTE_AFTER) is True
    assert resolve_provider_sort(a.llm._client) == "latency"
    assert resolve_provider_sort(b.llm._client) is None, "another session is untouched"
    import os
    assert "OPENROUTER_PROVIDER_SORT" not in os.environ


def test_reroute_is_idempotent(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_REROUTE", "true")
    a = _Agent()
    assert maybe_reroute(a, REROUTE_AFTER) is True
    assert maybe_reroute(a, REROUTE_AFTER + 1) is True
    assert resolve_provider_sort(a.llm._client) == "latency"


def test_the_sort_reaches_the_request_body(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_REROUTE", "true")
    a = _Agent()
    maybe_reroute(a, REROUTE_AFTER)
    params = {}
    apply_provider_routing(params, a.llm._client)
    assert params["extra_body"]["provider"]["sort"] == "latency"


def test_a_per_client_override_beats_the_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "price")
    c = _Client()
    assert resolve_provider_sort(c) == "price"
    set_provider_sort(c, "latency")
    assert resolve_provider_sort(c) == "latency"
    set_provider_sort(c, None)
    assert resolve_provider_sort(c) == "price", "clearing falls back to the env"


def test_an_unknown_sort_is_refused():
    c = _Client()
    assert set_provider_sort(c, "cheapest") is False
    assert resolve_provider_sort(c) is None


def test_the_third_timeout_stops_the_retry(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_REROUTE", "true")
    assert should_stop_retrying(STOP_AFTER - 1) is False
    assert should_stop_retrying(STOP_AFTER) is True
