"""BILLING_FAILOVER_ENABLED: a billing error attempts fallback instead of halting."""
import asyncio
import logging

from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
from core.exceptions import LLMPermanentError


class _State:
    def __init__(self):
        self.consecutive_failures = 0
        self.stopped = False
        self.llm_providers_failed = set()
        self.tracked = []
    def track_llm_error(self, error_type, provider):
        self.tracked.append((error_type, provider))
        if provider:
            self.llm_providers_failed.add(provider)
        return False
    def reset_llm_errors(self, **k): pass


class _Host(ErrorRecoveryMixin):
    def __init__(self, fallback_ok):
        self.logger = logging.getLogger("test_billing_failover")
        self.state = _State()
        self.max_failures = 3
        self.model_name = "gpt-5"
        self._fallback_ok = fallback_ok
    def _get_provider_from_model(self, m): return "openai"
    async def _recover_from_error(self, e): pass
    async def _attempt_llm_fallback_in_handler(self, et): return self._fallback_ok


def _billing_error():
    return Exception("Error code 402: insufficient_quota / billing hard limit reached")


def _real_openrouter_402_error():
    # I-5: the real prod shape (2026-07-09 outage) — contains "402"/"credits" but NEITHER
    # "insufficient_quota" NOR "billing". In production this reaches _handle_step_error
    # already wrapped as LLMPermanentError (modules/llm/llm_client.py::translate_llm_error
    # matches "402" and raises LLMPermanentError before the raw exception ever gets here),
    # so we construct the same typed shape here rather than a bare Exception.
    return LLMPermanentError(
        'from OpenRouterClient: 402 "This request requires more credits, or fewer '
        'max_tokens. visit https://openrouter.ai/settings/credits and add more credits"'
    )


def test_billing_halts_when_failover_disabled(monkeypatch):
    monkeypatch.setenv("BILLING_FAILOVER_ENABLED", "false")
    host = _Host(fallback_ok=True)
    res = asyncio.run(host._handle_step_error(_billing_error()))
    assert host.state.stopped is True
    assert "PERMANENT" in (res[0].error or "")


def test_billing_fails_over_by_default(monkeypatch):
    # I-5: BILLING_FAILOVER_ENABLED now defaults ON — this locks the new default in.
    monkeypatch.delenv("BILLING_FAILOVER_ENABLED", raising=False)
    host = _Host(fallback_ok=True)
    res = asyncio.run(host._handle_step_error(_billing_error()))
    assert host.state.stopped is False
    assert res == []          # empty result = continue with new provider


def test_real_openrouter_402_fails_over_when_enabled(monkeypatch):
    # I-5 acceptance criterion: the real prod-shape 402 (no literal "insufficient_quota"/
    # "billing") is detected as billing and attempts fallback when the flag is on.
    monkeypatch.setenv("BILLING_FAILOVER_ENABLED", "true")
    host = _Host(fallback_ok=True)
    res = asyncio.run(host._handle_step_error(_real_openrouter_402_error()))
    assert host.state.stopped is False
    assert res == []
    assert ("billing", "openai") in host.state.tracked


def test_billing_fails_over_when_enabled(monkeypatch):
    monkeypatch.setenv("BILLING_FAILOVER_ENABLED", "true")
    host = _Host(fallback_ok=True)
    res = asyncio.run(host._handle_step_error(_billing_error()))
    assert host.state.stopped is False
    assert res == []          # empty result = continue with new provider


def test_billing_accumulates_failed_provider(monkeypatch):
    # HIGH-2: the exhausted provider must be recorded so a later billing error excludes it
    # (prevents A↔B ping-pong). _get_provider_from_model returns "openai" in the stub host.
    monkeypatch.setenv("BILLING_FAILOVER_ENABLED", "true")
    host = _Host(fallback_ok=True)
    asyncio.run(host._handle_step_error(_billing_error()))
    assert ("billing", "openai") in host.state.tracked
    assert "openai" in host.state.llm_providers_failed


def test_billing_still_halts_if_no_fallback(monkeypatch):
    monkeypatch.setenv("BILLING_FAILOVER_ENABLED", "true")
    host = _Host(fallback_ok=False)
    res = asyncio.run(host._handle_step_error(_billing_error()))
    assert host.state.stopped is True
