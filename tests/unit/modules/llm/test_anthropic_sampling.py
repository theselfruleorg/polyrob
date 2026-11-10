"""Sampling params: never to the real API, only to a named compat seat, in ``extra_body``.

anthropic 1.6 removed `temperature`, `top_p` and `top_k` from `messages.create()`, and the real
API refuses them on current models. A third-party Anthropic-compatible endpoint still reads
them, so dropping them there would change every turn without a word.
"""
from types import SimpleNamespace

import pytest

from modules.llm import anthropic_sampling as s
from modules.llm.anthropic_client import AnthropicClient
from modules.llm.compat_anthropic import AnthropicCompatClient


def _params(**extra):
    return {"model": "m", "max_tokens": 8, "messages": [], "temperature": 0.7, **extra}


@pytest.mark.parametrize("name", ["temperature", "top_p", "top_k"])
def test_a_sampling_param_never_reaches_the_real_api(name):
    out = s.route_sampling({"model": "m", name: 0.5}, compat_seat=False)
    assert name not in out and "extra_body" not in out


@pytest.mark.parametrize("name", ["temperature", "top_p", "top_k"])
def test_a_compat_seat_carries_it_in_extra_body(name):
    out = s.route_sampling({"model": "m", name: 0.5}, compat_seat=True)
    assert name not in out and out["extra_body"] == {name: 0.5}


def test_a_caller_supplied_extra_body_value_wins():
    out = s.route_sampling(_params(extra_body={"temperature": 0.1, "k": 1}), compat_seat=True)
    assert out["extra_body"] == {"temperature": 0.1, "k": 1}


def test_other_params_are_never_touched():
    p = _params(stop_sequences=["x"], tools=[{"name": "t"}], system="s", tool_choice={"type": "auto"})
    out = s.route_sampling(p, compat_seat=False)
    for key in ("model", "max_tokens", "messages", "stop_sequences", "tools", "system", "tool_choice"):
        assert out[key] == p[key]


def test_the_caller_s_dict_is_not_mutated():
    p = _params()
    s.route_sampling(p, compat_seat=True)
    assert p["temperature"] == 0.7, "a shared params dict is retried on failure"


def test_absent_sampling_params_change_nothing():
    p = {"model": "m", "max_tokens": 8, "messages": []}
    assert s.route_sampling(p, compat_seat=True) is p


def _capture(cls):
    client = object.__new__(cls)
    client.model_type = "claude-sonnet-5-5"
    client.temperature = 0.7 if cls is AnthropicCompatClient else None
    client.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    client.max_tokens = 1024
    return client._build_tool_api_params(
        [{"role": "user", "content": "x"}], [{"name": "t", "input_schema": {"type": "object"}}],
        "sys", None, 16, {})


def _seat(name, monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = SimpleNamespace(get=lambda key, default=None: default,
                             get_llm_config=lambda: {name: {"api_key": "k", "model": "claude-sonnet-5-5"}})
    return AnthropicCompatClient(config=config, name=name)


def test_a_subscription_seat_on_the_real_api_sends_no_sampling_param(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_OAUTH_BASE_URL", raising=False)
    client = _seat("anthropic-oauth", monkeypatch)
    params = client._build_tool_api_params(
        [{"role": "user", "content": "x"}], [{"name": "t", "input_schema": {"type": "object"}}],
        "sys", None, 16, {})
    assert "temperature" not in params and "extra_body" not in params


def test_a_subscription_seat_through_a_gateway_keeps_its_sampling_param(monkeypatch):
    client = _seat("anthropic-oauth", monkeypatch, ANTHROPIC_OAUTH_BASE_URL="https://gateway.example.test")
    params = client._build_tool_api_params(
        [{"role": "user", "content": "x"}], [{"name": "t", "input_schema": {"type": "object"}}],
        "sys", None, 16, {})
    assert params["extra_body"] == {"temperature": 0.7}


def test_the_real_client_sends_no_temperature_through_a_gateway_too():
    params = _capture(AnthropicClient)
    assert "temperature" not in params and "extra_body" not in params


def test_the_real_client_drops_an_explicit_temperature():
    client = object.__new__(AnthropicClient)
    client.model_type = "claude-sonnet-5-5"
    client.temperature = None
    client.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    client.max_tokens = 1024
    params = client._build_tool_api_params(
        [{"role": "user", "content": "x"}], [{"name": "t", "input_schema": {"type": "object"}}],
        "sys", 0.0, 16, {})
    assert "temperature" not in params


def test_the_compat_seat_keeps_its_temperature():
    params = _capture(AnthropicCompatClient)
    assert "temperature" not in params and params["extra_body"]["temperature"] == 0.7


def test_the_real_client_has_no_default_temperature():
    from unittest.mock import Mock
    config = Mock()
    config.get_llm_config.return_value = {"anthropic": {"model": "claude-sonnet-5-5", "api_key": "k"}}
    assert AnthropicClient(config).temperature is None
