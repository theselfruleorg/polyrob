"""Autonomous dispatch must honor the operator provider pin.

Regression for the 2026-08-13 dormancy (part 2): commit 96aefdff made ``zai-coding``
the LLMManager primary client, but a goal dispatched AFTER that fix still built its
session on ``openrouter:deepseek-v3.2`` and 402'd. Root cause: the goal dispatcher,
the planner, and cron all resolved their provider via
``resolve_runtime_config(None, None)`` with NO ``pinned_provider`` argument, so they
fell to the "first keyed provider in canonical order" path — which picks the
canonical-first (dead, keyed) ``openrouter`` over the operator-pinned (funded)
``zai-coding``. The fix: a ``resolve_default_provider()`` helper that threads the
``CHAT_PROVIDER``/``DEFAULT_PROVIDER`` pin, used by every autonomous dispatch path.
"""

import os


def test_operator_provider_pin_reads_chat_then_default(monkeypatch):
    from core.runtime_config import operator_provider_pin

    monkeypatch.setenv("CHAT_PROVIDER", "chat-pinned")
    monkeypatch.setenv("DEFAULT_PROVIDER", "default-pinned")
    assert operator_provider_pin() == "chat-pinned"  # CHAT_PROVIDER wins

    monkeypatch.delenv("CHAT_PROVIDER")
    assert operator_provider_pin() == "default-pinned"

    monkeypatch.delenv("DEFAULT_PROVIDER")
    assert operator_provider_pin() is None


def test_resolve_default_provider_honors_pin(monkeypatch):
    """With the pin set to a keyed provider, resolve_default_provider returns it
    instead of the canonical-first provider."""
    from core.runtime_config import resolve_default_provider

    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")  # canonical-first, would win bare
    provider, _model = resolve_default_provider()
    assert provider == "zai-coding"


def test_dispatch_sources_use_resolve_default_provider():
    """The goal dispatcher, the planner, and cron runner must resolve the provider
    via the pin-honoring helper (resolve_default_provider), NOT a bare
    resolve_runtime_config(None, None) that ignores the operator pin."""
    import agents.task.goals.dispatcher as dispatcher_mod
    import cron.runner as cron_runner_mod
    from core.runtime_config import resolve_default_provider

    src_dispatcher = open(dispatcher_mod.__file__).read()
    src_cron = open(cron_runner_mod.__file__).read()

    # The pin-ignoring bare call must be GONE from the autonomous paths…
    assert "resolve_runtime_config(None, None)" not in src_dispatcher
    assert "resolve_runtime_config(None, None)" not in src_cron
    # …replaced by the pin-honoring helper.
    assert "resolve_default_provider" in src_dispatcher
    assert "resolve_default_provider" in src_cron
    # And the helper exists with the right signature.
    assert callable(resolve_default_provider)
