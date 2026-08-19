"""A durable job pinned to a dead provider must re-route, not die with it.

Prod's 3-hourly digest job stored `"provider":"zai-coding"` INSIDE its payload
when it was created on 2026-07-19. When that account hit its weekly cap on
2026-08-17 the job stopped running entirely — every tick logged
`provider-credit sentinel active for zai-coding — $0 skip` — even though the pin
was a month-old convenience, not an owner requirement for THAT provider.

A pin says "prefer this". It must not say "and never run again if this dies".
"""
import pytest

from core.runtime_config import resolve_live_provider


@pytest.fixture
def _providers(monkeypatch):
    """Two providers with usable credentials, in canonical order."""
    import core.runtime_config as rc
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["zai-coding", "openrouter"], raising=False)
    return rc


def _dead(*names):
    dead = set(names)

    def _active(provider=None):
        if provider is None:
            return bool(dead)
        return provider in dead
    return _active


def test_a_healthy_pin_is_honoured(_providers, monkeypatch):
    monkeypatch.setattr(_providers, "_sentinel_active", _dead(), raising=False)
    assert resolve_live_provider("zai-coding") == "zai-coding"


def test_a_dead_pin_reroutes_to_a_live_provider(_providers, monkeypatch):
    monkeypatch.setattr(_providers, "_sentinel_active", _dead("zai-coding"), raising=False)
    assert resolve_live_provider("zai-coding") == "openrouter"


def test_all_dead_returns_none_so_the_caller_can_skip(_providers, monkeypatch):
    monkeypatch.setattr(_providers, "_sentinel_active",
                        _dead("zai-coding", "openrouter"), raising=False)
    assert resolve_live_provider("zai-coding") is None


def test_no_preference_picks_the_first_live_provider(_providers, monkeypatch):
    monkeypatch.setattr(_providers, "_sentinel_active", _dead("zai-coding"), raising=False)
    assert resolve_live_provider(None) == "openrouter"


def test_an_unkeyed_pin_still_reroutes_rather_than_stalling(_providers, monkeypatch):
    """A pin naming a provider with no credentials at all is equally a dead end."""
    monkeypatch.setattr(_providers, "_sentinel_active", _dead(), raising=False)
    assert resolve_live_provider("deepseek") == "zai-coding"
