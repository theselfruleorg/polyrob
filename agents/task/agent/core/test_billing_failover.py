"""BILLING_FAILOVER_ENABLED: a billing error attempts fallback instead of halting."""
import asyncio
import logging

from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
from core.exceptions import LLMPermanentError


import pytest


@pytest.fixture(autouse=True)
def _isolate_data_home(tmp_path, monkeypatch):
    """These tests drive _handle_step_error with realistic 402 text, which (since
    the universal trip site) writes a REAL CREDIT_SENTINEL latch — without this
    isolation it lands in the developer's .polyrob/ and silently pauses every
    dispatcher test on the box (live pollution incident, 2026-07-18)."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))


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


def test_trip_sentinel_fires_on_real_402_permanent_error(monkeypatch):
    """The sentinel trip itself must accept the real prod shape (LLMPermanentError
    carrying 402/credits text)."""
    calls = []

    async def fake_trip(reason, **k):
        calls.append(reason)

    import core.credit_sentinel as cs
    monkeypatch.setattr(cs, "trip_credit_sentinel", fake_trip)
    host = _Host(fallback_ok=False)
    err = LLMPermanentError(
        "OpenRouter: Error code: 402 - This request requires more credits")
    asyncio.run(host._trip_sentinel_if_credit_death(err))
    assert calls, "sentinel must trip on a 402 LLMPermanentError"
    assert "402" in calls[0]


def test_fatal_halt_branch_trips_credit_sentinel_source():
    """2026-07-18 live finding: with billing failover OFF (the default) a
    credit-death error is classified FATAL and the step loop returns BEFORE
    _handle_step_error — so the 'ONE universal trip site' was unreachable for
    the exact prod 402 it exists to catch (a full day of 402 storms, zero
    trips). The fatal branch must trip the sentinel itself before halting."""
    import inspect
    import re
    import agents.task.agent.core.step as step_mod
    src = inspect.getsource(step_mod)
    assert "if is_fatal_error:" in src
    fatal_block = src.split("if is_fatal_error:", 1)[1]
    trip_pos = fatal_block.find("await self._trip_sentinel_if_credit_death")
    ret = re.search(r"^\s*return\s*$", fatal_block, re.MULTILINE)
    assert trip_pos != -1, "fatal branch must call _trip_sentinel_if_credit_death"
    assert ret is not None and trip_pos < ret.start(), \
        "trip must happen before the fatal-branch return statement"


def test_trip_sentinel_walks_wrapper_context_chain(monkeypatch):
    """Live 2026-07-18 shape: llm_runner re-wraps the 402 as
    LLMProviderExhaustedError('No fallback available after LLMPermanentError')
    — no billing text on the wrapper. The trip classifier must walk
    __cause__/__context__ to find the 402 underneath."""
    from core.exceptions import LLMProviderExhaustedError

    calls = []

    async def fake_trip(reason, **k):
        calls.append(reason)

    import core.credit_sentinel as cs
    monkeypatch.setattr(cs, "trip_credit_sentinel", fake_trip)
    host = _Host(fallback_ok=False)
    try:
        try:
            raise LLMPermanentError(
                "OpenRouter: Error code: 402 - This request requires more credits")
        except LLMPermanentError:
            # deliberately NO `from` — relies on implicit __context__, the
            # historical raise shape
            raise LLMProviderExhaustedError(
                "No fallback available after LLMPermanentError")
    except LLMProviderExhaustedError as wrapper:
        asyncio.run(host._trip_sentinel_if_credit_death(wrapper))
    assert calls, "sentinel must trip via the exception chain"
    assert "402" in calls[0], "latch reason must carry the matched billing text"


def test_trip_sentinel_ignores_benign_wrapper_chain(monkeypatch):
    """A wrapper whose whole chain is billing-free must NOT trip."""
    from core.exceptions import LLMProviderExhaustedError

    calls = []

    async def fake_trip(reason, **k):
        calls.append(reason)

    import core.credit_sentinel as cs
    monkeypatch.setattr(cs, "trip_credit_sentinel", fake_trip)
    host = _Host(fallback_ok=False)
    try:
        try:
            raise LLMPermanentError("model not found on this endpoint")
        except LLMPermanentError:
            raise LLMProviderExhaustedError(
                "No fallback available after LLMPermanentError")
    except LLMProviderExhaustedError as wrapper:
        asyncio.run(host._trip_sentinel_if_credit_death(wrapper))
    assert not calls
