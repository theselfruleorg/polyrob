"""`OPENROUTER_PROVIDER_ORDER` / `_IGNORE`: the request names upstreams."""
from modules.llm.openrouter_routing import apply_provider_routing, provider_routing_extra_body


def _clear(monkeypatch):
    for name in ("OPENROUTER_PROVIDER_SORT", "OPENROUTER_PROVIDER_ORDER", "OPENROUTER_PROVIDER_IGNORE"):
        monkeypatch.delenv(name, raising=False)


def test_unset_sends_no_block(monkeypatch):
    _clear(monkeypatch)
    assert provider_routing_extra_body() is None
    assert apply_provider_routing({"model": "m"}) == {"model": "m"}


def test_order_and_ignore_are_lists_and_fallback_stays_open(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENROUTER_PROVIDER_ORDER", "UpA, UpB,")
    monkeypatch.setenv("OPENROUTER_PROVIDER_IGNORE", "UpC")
    provider = provider_routing_extra_body()["provider"]
    assert provider == {"order": ["UpA", "UpB"], "ignore": ["UpC"]}
    assert "allow_fallbacks" not in provider


def test_the_sort_and_the_order_go_together(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "latency")
    monkeypatch.setenv("OPENROUTER_PROVIDER_ORDER", "UpA")
    assert provider_routing_extra_body()["provider"] == {"sort": "latency", "order": ["UpA"]}
