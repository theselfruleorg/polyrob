"""067 P1b: the rail-neutral prepaid-request context (CR-M15 semantics)."""
from core import billing_context as bc


def test_default_is_not_prepaid():
    assert bc.is_prepaid() is False


def test_mark_and_reset(monkeypatch):
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "1234")
    token = bc.mark_prepaid()
    try:
        assert bc.is_prepaid() is True
        assert bc.prepaid_budget() == 1234
    finally:
        bc.reset_prepaid(token)
    assert bc.is_prepaid() is False


def test_an_explicit_budget_wins(monkeypatch):
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "1234")
    token = bc.mark_prepaid(99)
    try:
        assert bc.prepaid_budget() == 99
    finally:
        bc.reset_prepaid(token)


def test_bad_budget_values_are_the_default(monkeypatch):
    for raw in ("0", "-5", "lots", ""):
        monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", raw)
        assert bc.prepaid_token_budget() == bc.DEFAULT_PREPAID_TOKEN_BUDGET


def test_the_x402_wrappers_share_the_one_context(monkeypatch):
    from modules.x402.x402_integration import (
        get_x402_max_tokens_per_request, is_x402_paid_request,
        mark_x402_paid_request, reset_x402_paid_request)
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "777")
    assert get_x402_max_tokens_per_request() == 777
    token = mark_x402_paid_request()
    try:
        assert bc.is_prepaid() and is_x402_paid_request()
    finally:
        reset_x402_paid_request(token)
    assert not is_x402_paid_request()
