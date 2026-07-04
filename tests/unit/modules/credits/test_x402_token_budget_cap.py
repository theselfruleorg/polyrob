"""S6 — x402 hard token cap. An x402 request prepays a fixed price for a bounded
token budget (X402_MAX_TOKENS_PER_REQUEST); once a session's cumulative tokens
exceed it, further LLM usage halts (InsufficientCreditsError) so actual cost can
never exceed what was prepaid.
"""
import logging
import types

import pytest

from modules.credits.usage_tracker import LLMUsageTracker
from core.exceptions import InsufficientCreditsError


def _tracker():
    t = LLMUsageTracker.__new__(LLMUsageTracker)
    t.logger = logging.getLogger("x402-cap-test")
    t._x402_session_tokens = {}
    t._tier_cache = {}
    return t


# ── _enforce_x402_budget (pure cap logic) ────────────────────────────────────

def test_budget_halts_when_cumulative_exceeds(monkeypatch):
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "1000")
    t = _tracker()
    t._enforce_x402_budget("u", "s1", 600)          # 600 total, under budget
    with pytest.raises(InsufficientCreditsError):
        t._enforce_x402_budget("u", "s1", 600)      # 1200 > 1000 -> halt


def test_budget_is_per_session(monkeypatch):
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "1000")
    t = _tracker()
    t._enforce_x402_budget("u", "s1", 900)
    t._enforce_x402_budget("u", "s2", 900)          # different session -> independent budget
    assert t._x402_session_tokens == {"s1": 900, "s2": 900}


def test_budget_accumulates_across_calls(monkeypatch):
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "1000")
    t = _tracker()
    t._enforce_x402_budget("u", "s1", 300)
    t._enforce_x402_budget("u", "s1", 300)
    assert t._x402_session_tokens["s1"] == 600
    with pytest.raises(InsufficientCreditsError):
        t._enforce_x402_budget("u", "s1", 500)      # 1100 > 1000


# ── _get_user_tier caching ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tier_is_cached_after_first_lookup():
    t = _tracker()
    calls = {"n": 0}

    class _DB:
        async def fetch_one(self, *a, **k):
            calls["n"] += 1
            return {"tier": "x402"}

    t.db = _DB()
    assert await t._get_user_tier("u") == "x402"
    assert await t._get_user_tier("u") == "x402"
    assert calls["n"] == 1  # second call served from cache


# ── integration through record_llm_usage ─────────────────────────────────────

@pytest.mark.asyncio
async def test_record_llm_usage_halts_x402_over_budget(monkeypatch):
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "1000")
    t = _tracker()
    t._tier_cache = {"usr_x": "x402"}
    t._validate_inputs = lambda i, o, c: c
    t._generate_request_id = lambda: "r1"

    async def _noop(*a, **k):
        return None

    t._write_to_database = _noop
    t._write_to_telemetry = _noop
    t._record_usage_ledger = _noop

    async def _costs(model, tokens):
        return types.SimpleNamespace(
            credits_charged=5, api_cost_usd=0.1, user_cost_usd=0.2, markup_multiplier=1.0)

    t._calculate_costs = _costs

    kw = dict(user_id="usr_x", session_id="s1", agent_id="a", model="m", provider="p")
    await t.record_llm_usage(input_tokens=400, output_tokens=200, **kw)   # 600 total, ok
    with pytest.raises(InsufficientCreditsError):
        await t.record_llm_usage(input_tokens=400, output_tokens=200, **kw)  # 1200 > 1000


@pytest.mark.asyncio
async def test_record_llm_usage_admin_not_capped(monkeypatch):
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "1000")
    t = _tracker()
    t._tier_cache = {"admin_u": "admin"}
    t._validate_inputs = lambda i, o, c: c
    t._generate_request_id = lambda: "r1"

    async def _noop(*a, **k):
        return None

    t._write_to_database = _noop
    t._write_to_telemetry = _noop
    t._record_usage_ledger = _noop

    async def _costs(model, tokens):
        return types.SimpleNamespace(
            credits_charged=5, api_cost_usd=0.1, user_cost_usd=0.2, markup_multiplier=1.0)

    t._calculate_costs = _costs

    kw = dict(user_id="admin_u", session_id="s1", agent_id="a", model="m", provider="p")
    # Admin is exempt AND uncapped — 3× over the budget must not raise.
    for _ in range(3):
        await t.record_llm_usage(input_tokens=400, output_tokens=200, **kw)
