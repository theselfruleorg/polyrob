"""Goal dispatch pauses only when NOTHING can serve — not when the default dies.

From 2026-08-17 19:35Z prod dispatched zero goals: the default provider
(zai-coding) was credit-dead, so the whole dispatch gate returned 0 on every
tick. Whether a SECOND credentialed provider was alive was never asked.

A per-goal provider pin gets the same treatment as a cron pin: prefer it, but do
not stop working forever because a month-old preference died.
"""
import pytest

import core.runtime_config as rc
from agents.task.goals.dispatcher import GoalDispatcher


@pytest.fixture
def two_providers(monkeypatch):
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["zai-coding", "openrouter"], raising=False)


def _dead(*names):
    dead = set(names)
    return lambda provider=None: (bool(dead) if provider is None else provider in dead)


def test_dispatch_is_not_paused_while_a_live_provider_remains(two_providers, monkeypatch):
    monkeypatch.setattr(rc, "_sentinel_active", _dead("zai-coding"), raising=False)
    assert GoalDispatcher.dispatch_blocked_by_providers() is False


def test_dispatch_pauses_when_every_provider_is_dead(two_providers, monkeypatch):
    import core.credit_sentinel as cs
    monkeypatch.setattr(rc, "_sentinel_active",
                        _dead("zai-coding", "openrouter"), raising=False)
    # Pausing needs a genuinely tripped latch, not merely an empty live set.
    monkeypatch.setattr(cs, "credit_sentinel_active", lambda provider=None: True,
                        raising=False)
    assert GoalDispatcher.dispatch_blocked_by_providers() is True


def test_a_goals_dead_provider_pin_reroutes(two_providers, monkeypatch):
    monkeypatch.setattr(rc, "_sentinel_active", _dead("zai-coding"), raising=False)
    assert rc.resolve_live_provider("zai-coding") == "openrouter"
