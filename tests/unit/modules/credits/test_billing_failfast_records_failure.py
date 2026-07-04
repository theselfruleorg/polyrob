"""H3: on the default fail-fast path (fail_on_insufficient=True), _deduct_from_balance
raised InsufficientCreditsError WITHOUT recording a billing_failures row — so a charge
that never deducted had NO reconciliation trail (contradicting the documented
"Billing Failures tracked for admin reconciliation" guarantee). The soft-fail branch
recorded it; the fail-fast branch must too, before raising.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from modules.credits.usage_tracker import LLMUsageTracker
from core.exceptions import InsufficientCreditsError


class _Balance:
    def __init__(self, deduct_ok):
        self._ok = deduct_ok

    async def deduct_credits(self, **kwargs):
        return self._ok

    async def get_balance(self, user_id):
        return {"balance": 0}


class _DB:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), tuple(params)))


def _tracker(fail_fast):
    t = LLMUsageTracker.__new__(LLMUsageTracker)
    t.balance = _Balance(deduct_ok=False)  # deduction always fails (insufficient)
    t.db = _DB()
    t.fail_on_insufficient = fail_fast
    t.logger = logging.getLogger("test.usage_tracker")
    return t


def _record(credits_charged=5):
    return SimpleNamespace(
        user_id="u1",
        session_id="s1",
        request_id="r1",
        model="gpt-5",
        tokens=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        costs=SimpleNamespace(credits_charged=credits_charged, api_cost_usd=0.01),
    )


def test_fail_fast_records_billing_failure_before_raising():
    t = _tracker(fail_fast=True)
    with pytest.raises(InsufficientCreditsError):
        asyncio.run(t._deduct_from_balance(_record()))
    assert any(
        "INSERT INTO billing_failures" in sql for (sql, _) in t.db.executed
    ), "fail-fast must leave a billing_failures reconciliation row"


def test_soft_fail_still_records_billing_failure():
    t = _tracker(fail_fast=False)
    asyncio.run(t._deduct_from_balance(_record()))  # must not raise
    assert any("INSERT INTO billing_failures" in sql for (sql, _) in t.db.executed)
