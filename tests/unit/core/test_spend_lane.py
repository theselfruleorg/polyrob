"""Tests for the tiered defi spend lane (proposal 023 §5.3 D3).

Owner decision 2026-08-19: a within-ceiling on-chain trade executes without a
pre-approval tap; above the ceiling it still queues. The dry-run exemption is
unconditional — `_run_guarded` returns before `sign_and_send`, so a simulation
can never move money and blocking it only cost the agent its ability to quote.
"""
import pytest

from core.config_policy.spend_lane import defi_spend_exemption


def _live(**kw):
    p = {"chain": "base", "dry_run": False, "max_spend_usd": 0.5}
    p.update(kw)
    return p


class TestDryRunAlwaysExempt:
    """A simulation broadcasts nothing, in any mode, flag on or off."""

    @pytest.mark.parametrize("verb", [
        "defi_trade_swap", "defi_trade_transfer",
        "defi_trade_approve_token", "defi_trade_revoke_approval",
    ])
    def test_explicit_dry_run_is_exempt(self, verb, monkeypatch):
        monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
        reason = defi_spend_exemption(verb, {"dry_run": True, "max_spend_usd": 999.0})
        assert reason and "dry" in reason.lower()

    def test_absent_dry_run_is_treated_as_dry_run(self, monkeypatch):
        """Every param model defaults dry_run=True, so absent means simulate."""
        monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
        assert defi_spend_exemption("defi_trade_swap", {"max_spend_usd": 999.0})


class TestFlagOffKeepsTheOwnerQueue:
    def test_live_spend_is_not_exempt_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("DEFI_TIERED_SPEND_LANE", raising=False)
        assert defi_spend_exemption("defi_trade_swap", _live()) is None

    def test_live_spend_is_not_exempt_when_flag_explicitly_off(self, monkeypatch):
        monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "false")
        assert defi_spend_exemption("defi_trade_swap", _live()) is None


class TestFlagOnTiersByDeclaredCeiling:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
        monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "1")

    def test_within_ceiling_is_exempt(self):
        assert defi_spend_exemption("defi_trade_swap", _live(max_spend_usd=0.9))

    def test_at_the_ceiling_is_exempt(self):
        assert defi_spend_exemption("defi_trade_swap", _live(max_spend_usd=1.0))

    def test_above_the_ceiling_still_queues(self):
        assert defi_spend_exemption("defi_trade_swap", _live(max_spend_usd=1.01)) is None

    def test_missing_declaration_still_queues(self):
        p = _live(); p.pop("max_spend_usd")
        assert defi_spend_exemption("defi_trade_swap", p) is None

    def test_non_numeric_declaration_still_queues(self):
        assert defi_spend_exemption("defi_trade_swap", _live(max_spend_usd="cheap")) is None

    def test_revoke_is_exempt_without_a_declaration(self):
        """revoke_approval sets an allowance to ZERO — it can only reduce risk."""
        assert defi_spend_exemption(
            "defi_trade_revoke_approval", {"dry_run": False}) is not None

    def test_a_non_defi_money_verb_is_never_exempt(self):
        for verb in ("x402_pay_pay", "hyperliquid_place_market_order",
                     "polymarket_place_limit_order"):
            assert defi_spend_exemption(verb, _live()) is None


class TestParamModelDefaultsArePinned:
    """The absent-dry_run exemption is only sound while the models default True."""

    def test_dry_run_defaults_true_on_every_spend_model(self):
        from tools.defi.trade_tool import (
            ApproveParams, RevokeParams, SwapParams, TransferParams)
        for model in (SwapParams, TransferParams, ApproveParams, RevokeParams):
            assert model.model_fields["dry_run"].default is True, model.__name__
