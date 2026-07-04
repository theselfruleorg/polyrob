"""C1: kill double-billing.

Today a credit-paying request is billed TWICE — once by the API gate
(verify_payment_for_request, api/payment_verification.py ~86-90) and again by the
per-token LLMUsageTracker (modules/credits/usage_tracker.py ~244-248) once the LLM
call completes. Decision: LLMUsageTracker is the SINGLE deduction path; the gate is
authorize-only.

This test simulates one full credit-paying request end-to-end (gate -> LLM call ->
usage tracker) against a fake balance manager that counts deduct_credits calls, and
asserts EXACTLY ONE deduction happens for the whole request.
"""
import pytest
from fastapi import Request

from api.payment_verification import verify_payment_for_request
from modules.credits.usage_tracker import LLMUsageTracker


class _FakeBalanceManager:
    def __init__(self, balance=1000):
        self.balance = balance
        self.deduct_calls = []

    async def has_sufficient_balance(self, user_id, amount):
        return self.balance >= amount

    async def deduct_credits(self, user_id, amount, reason, session_id=None):
        self.deduct_calls.append((user_id, amount, reason))
        self.balance -= amount
        return True

    async def get_balance(self, user_id):
        return {"balance": self.balance}


class _FakeDB:
    """Minimal async DB stub — usage_tracker writes usage_records + reads tier."""

    async def execute(self, *a, **k):
        return None

    async def fetch_one(self, *a, **k):
        # No user_profiles row -> _should_deduct_credits defaults to True (deduct).
        return None


class _FakeContainer:
    def __init__(self, balance_mgr):
        self._balance_mgr = balance_mgr
        self.config = None

    def get_service(self, name):
        if name == "balance_manager":
            return self._balance_mgr
        return None


def _fake_request(user_id="u1"):
    scope = {
        "type": "http", "method": "POST", "path": "/task/sessions",
        "headers": [], "query_string": b"",
    }
    req = Request(scope)
    req.state.user_id = user_id
    req.state.role = "user"
    req.state.tier = "free"
    req.state.payment_method = None
    return req


@pytest.mark.asyncio
async def test_one_request_deducts_credits_exactly_once(monkeypatch):
    balance_mgr = _FakeBalanceManager(balance=1000)
    container = _FakeContainer(balance_mgr)

    from core.container import DependencyContainer
    monkeypatch.setattr(
        DependencyContainer, "get_instance",
        classmethod(lambda cls, *a, **k: container),
    )

    # 1) The gate — must NOT deduct (authorize-only after the fix).
    request = _fake_request()
    method, details = await verify_payment_for_request(request, cost_credits=1)
    assert method == "credits"

    # 2) The per-token tracker — the ONLY deduction path after the fix.
    tracker = LLMUsageTracker(db=_FakeDB(), balance_manager=balance_mgr, telemetry_manager=None)
    await tracker.record_llm_usage(
        user_id="u1", session_id="s1", agent_id="a1",
        model="gpt-4o-mini", provider="openai",
        input_tokens=100, output_tokens=50,
    )

    assert len(balance_mgr.deduct_calls) == 1, (
        f"expected exactly one deduction for the whole request, got "
        f"{len(balance_mgr.deduct_calls)}: {balance_mgr.deduct_calls}"
    )
