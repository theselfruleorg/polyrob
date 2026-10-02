"""CR-M15: a wallet that paid x402 ONCE keeps tier='x402' on its profile.
Billing must not treat that stored tier as prepaid: a later SIWE/JWT call
under the same user pays per token. Only a request that settled an x402
payment (the middleware's context flag) — or the session it started — is
prepaid and budget-capped.
"""
import asyncio
import logging
import types

import pytest

from modules.credits.usage_tracker import LLMUsageTracker
from modules.x402.x402_integration import (
    is_x402_paid_request, mark_x402_paid_request, reset_x402_paid_request)


def _tracker():
    t = LLMUsageTracker.__new__(LLMUsageTracker)
    t.logger = logging.getLogger("cr-m15")
    t._x402_session_tokens = {}
    t._tier_cache = {"usr_x": "x402"}   # the permanent tier a one-time payer keeps
    t._validate_inputs = lambda i, o, c: c
    t._generate_request_id = lambda: "r1"
    t.deducted = []

    async def _noop(*a, **k):
        return None

    async def _deduct(record):
        t.deducted.append(record.costs.credits_charged)

    t._write_to_database = _noop
    t._write_to_telemetry = _noop
    t._record_usage_ledger = _noop
    t._deduct_from_balance = _deduct
    t.balance = object()

    async def _costs(model, tokens, provider=None):
        return types.SimpleNamespace(
            credits_charged=5, api_cost_usd=0.1, user_cost_usd=0.2, markup_multiplier=1.0)

    t._calculate_costs = _costs
    return t


KW = dict(user_id="usr_x", agent_id="a", model="m", provider="p",
          input_tokens=10, output_tokens=10)


@pytest.mark.asyncio
async def test_stored_x402_tier_without_a_paid_request_is_billed():
    t = _tracker()
    await t.record_llm_usage(session_id="siwe_session", **KW)
    assert t.deducted == [5], "a stored x402 tier was treated as prepaid"


@pytest.mark.asyncio
async def test_settled_request_is_prepaid_and_its_session_stays_prepaid():
    t = _tracker()
    token = mark_x402_paid_request()
    try:
        await t.record_llm_usage(session_id="paid_s", **KW)
    finally:
        reset_x402_paid_request(token)
    assert t.deducted == []
    # A later call in the SAME paid session (outside the request) stays prepaid.
    await t.record_llm_usage(session_id="paid_s", **KW)
    assert t.deducted == []
    # A different session of the same user is billed.
    await t.record_llm_usage(session_id="other", **KW)
    assert t.deducted == [5]


@pytest.mark.asyncio
async def test_paid_flag_inherits_into_spawned_session_task():
    token = mark_x402_paid_request()
    try:
        task = asyncio.create_task(asyncio.sleep(0, result=None))
        inner = asyncio.create_task(_read_flag())
        await task
        assert await inner is True
    finally:
        reset_x402_paid_request(token)
    assert is_x402_paid_request() is False


async def _read_flag():
    return is_x402_paid_request()
