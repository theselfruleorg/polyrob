"""`OPENROUTER_PROVIDER_SORT` — ask OpenRouter to pick the upstream for the SAME
model by latency / throughput / price.

Prod 2026-09-19: the owner's seat (`deepseek/deepseek-v4.1-flash` via OpenRouter)
hit four 290–410 s LLM timeouts and five money-rail cap cuts in one afternoon.
OpenRouter's default routing is price-first; this knob lets ops prefer the
fastest upstream without touching the model seat. Unset ⇒ byte-identical
request (no `extra_body` at all).
"""
import pytest

from modules.llm.openrouter_routing import (
    _PROVIDER_SORT_VALUES,
    apply_provider_routing,
    provider_routing_extra_body,
)


@pytest.mark.parametrize("raw", ["", "   ", "0", "off", "none", "false", "random", "LATENCYX"])
def test_unset_or_unknown_value_adds_nothing(monkeypatch, raw):
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", raw)
    assert provider_routing_extra_body() is None
    params = {"model": "m", "messages": []}
    assert apply_provider_routing(params) is params
    assert "extra_body" not in params


def test_unset_env_adds_nothing(monkeypatch):
    monkeypatch.delenv("OPENROUTER_PROVIDER_SORT", raising=False)
    assert provider_routing_extra_body() is None


@pytest.mark.parametrize("raw", sorted(_PROVIDER_SORT_VALUES))
def test_known_sort_value_rides_in_extra_body(monkeypatch, raw):
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", f" {raw.upper()} ")
    body = provider_routing_extra_body()
    assert body == {"provider": {"sort": raw}}
    params = apply_provider_routing({"model": "m"})
    assert params["extra_body"] == {"provider": {"sort": raw}}


def test_existing_extra_body_is_merged_not_replaced(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "latency")
    params = apply_provider_routing({"extra_body": {"transforms": ["middle-out"]}})
    assert params["extra_body"] == {"transforms": ["middle-out"], "provider": {"sort": "latency"}}
