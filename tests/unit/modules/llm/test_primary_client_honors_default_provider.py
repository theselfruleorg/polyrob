"""LLMManager primary-client selection must honor the operator provider pin.

Regression for the 2026-08-13 dormancy: OpenRouter was exhausted (402), the
owner had pinned a funded z.ai key, but ``_set_primary_client`` used a hardcoded
``priority_order`` that never includes dynamically-registered providers like
``zai-coding``. So a *dead-but-keyed* OpenRouter was always chosen primary and
its 402 tripped the provider-credit sentinel before z.ai was ever used — Rob sat
dormant for ~24 days. The operator's ``DEFAULT_PROVIDER``/``CHAT_PROVIDER`` pin
(already honored by ``resolve_runtime_config`` for goal dispatch + chat) must
also win at the LLM-manager primary-client layer.
"""

import os

import pytest


class _FakeClient:
    """Minimal stand-in — only ``name`` is read by the selection logic."""

    def __init__(self, name):
        self.name = name


def _manager_with_clients(monkeypatch, clients, config=None):
    """Build an LLMManager instance with ``self.clients`` + config pre-set, WITHOUT
    running ``initialize()`` (which would make real API calls)."""
    from core.config import BotConfig
    from modules.llm.llm_manager import LLMManager

    cfg = config if config is not None else BotConfig()
    mgr = LLMManager.__new__(LLMManager)  # bypass __init__ (no container/clients built)
    mgr.config = cfg
    mgr.clients = {name: _FakeClient(name) for name in clients}
    mgr.logger = __import__("logging").getLogger("test")
    mgr.primary_client_name = None
    # `_set_primary_client` only reads env + self.clients/self.config, so this is enough.
    return mgr


def test_pinned_provider_becomes_primary_over_dead_openrouter(monkeypatch):
    """DEFAULT_PROVIDER=zai-coding + a present (but here dead) openrouter client →
    zai-coding is primary, NOT openrouter from the hardcoded order."""
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    mgr = _manager_with_clients(
        monkeypatch,
        clients=["openrouter_client", "zai-coding_client"],
    )
    import asyncio

    asyncio.run(mgr._set_primary_client())
    assert mgr.primary_client_name == "zai-coding_client"


def test_chat_provider_outranks_default_provider(monkeypatch):
    """CHAT_PROVIDER is the higher-precedence operator pin."""
    monkeypatch.setenv("CHAT_PROVIDER", "zai-coding")
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    mgr = _manager_with_clients(
        monkeypatch,
        clients=["openrouter_client", "zai-coding_client"],
    )
    import asyncio

    asyncio.run(mgr._set_primary_client())
    assert mgr.primary_client_name == "zai-coding_client"


def test_unkeyed_or_absent_pinned_provider_falls_through_to_order(monkeypatch):
    """A pin whose client isn't registered must fall through to the hardcoded
    order (fail-open, never pick a missing client)."""
    monkeypatch.setenv("DEFAULT_PROVIDER", "nvidia")  # not in self.clients here
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    mgr = _manager_with_clients(
        monkeypatch,
        clients=["openrouter_client", "gemini_client"],
    )
    import asyncio

    asyncio.run(mgr._set_primary_client())
    # openrouter precedes gemini in the hardcoded order
    assert mgr.primary_client_name == "openrouter_client"


def test_no_pin_keeps_legacy_order(monkeypatch):
    """With no operator pin, the hardcoded priority order is byte-identical (openai
    first). Guards against a regression that would change the default."""
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    mgr = _manager_with_clients(
        monkeypatch,
        clients=["gemini_client", "openai_client", "openrouter_client"],
    )
    import asyncio

    asyncio.run(mgr._set_primary_client())
    assert mgr.primary_client_name == "openai_client"
