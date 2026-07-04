"""F12 (P1-1): one source of truth for the x402 per-request price.

The live middleware charge ($0.01), the /pricing endpoint ($0.015/$0.005) and the
Agent Card all disagreed. Drive them from one config-backed function so they can
never diverge.
"""
import pytest

from modules.x402.x402_integration import (
    get_x402_price_usd,
    get_x402_max_tokens_per_request,
    _max_output_price_per_token,
)


def test_default_price_derived_from_model_economics(monkeypatch):
    """S6: with no explicit X402_PRICE_USD the price is derived so a request can never
    cost the platform more than it collects: budget × max-output-rate × markup."""
    monkeypatch.delenv("X402_PRICE_USD", raising=False)
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "200000")
    monkeypatch.setenv("X402_PRICE_MARKUP", "2.0")
    expected = 200000 * _max_output_price_per_token() * 2.0
    assert _max_output_price_per_token() > 0  # registry has priced models
    assert get_x402_price_usd() == pytest.approx(round(expected, 6))
    # It is materially more than the old flat $0.01 (that was the leak).
    assert get_x402_price_usd() > 1.0


def test_budget_and_markup_scale_the_price(monkeypatch):
    monkeypatch.delenv("X402_PRICE_USD", raising=False)
    monkeypatch.setenv("X402_MAX_TOKENS_PER_REQUEST", "50000")
    monkeypatch.setenv("X402_PRICE_MARKUP", "3.0")
    expected = 50000 * _max_output_price_per_token() * 3.0
    assert get_x402_price_usd() == pytest.approx(round(expected, 6))


def test_explicit_price_still_wins(monkeypatch):
    monkeypatch.setenv("X402_PRICE_USD", "0.05")
    assert get_x402_price_usd() == 0.05


def test_card_price_matches_price_source(monkeypatch):
    monkeypatch.setenv("X402_PRICE_USD", "0.07")
    from api.a2a.agent_card import build_agent_card
    card = build_agent_card()
    assert card.pricing["authentication_options"]["x402"]["per_request_usd"] == 0.07


def test_invalid_explicit_price_derives_not_one_cent(monkeypatch):
    """An invalid explicit price must NOT silently fall back to the leaky $0.01 —
    it derives the safe economics-based price instead."""
    monkeypatch.setenv("X402_PRICE_USD", "not-a-number")
    monkeypatch.delenv("X402_MAX_TOKENS_PER_REQUEST", raising=False)
    monkeypatch.delenv("X402_PRICE_MARKUP", raising=False)
    assert get_x402_price_usd() > 1.0


def test_budget_default_is_200k(monkeypatch):
    monkeypatch.delenv("X402_MAX_TOKENS_PER_REQUEST", raising=False)
    assert get_x402_max_tokens_per_request() == 200000
