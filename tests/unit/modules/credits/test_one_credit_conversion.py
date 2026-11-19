"""One conversion of a provider cost to credits, in decimal (088 R4)."""
import pytest

from modules.credits.pricing import PricingConfig


def test_an_exact_cost_is_not_one_credit_dearer():
    # float: 0.07 / 0.01 == 7.000000000000001, and its ceiling is 8
    assert PricingConfig.credits_for_api_cost(0.07)[0] == 7
    assert PricingConfig.credits_for_api_cost(0.29)[0] == 29


def test_a_charge_rounds_up_and_keeps_the_minimum():
    assert PricingConfig.credits_for_api_cost(0.0701)[0] == 8
    assert PricingConfig.credits_for_api_cost(0.0)[0] == PricingConfig.MIN_CREDIT_CHARGE


def test_the_markup_applies():
    assert PricingConfig.credits_for_api_cost(0.10, markup=1.5)[0] == 15


@pytest.mark.parametrize("rate", [0, -0.01, float("nan")])
def test_a_rate_that_is_not_positive_refuses(monkeypatch, rate):
    monkeypatch.setattr(PricingConfig, "CREDIT_VALUE_USD", rate)
    with pytest.raises(ValueError):
        PricingConfig.credits_for_api_cost(0.10)


def test_the_tracker_and_the_reservation_use_the_one_conversion(monkeypatch):
    from modules.credits import usage_tracker as ut
    monkeypatch.setattr(PricingConfig, "CREDIT_VALUE_USD", 0.02)
    costs = ut.LLMUsageTracker._apply_billed_cost(
        ut.CostBreakdown(api_cost_usd=0.0, markup_multiplier=1.0, credits_raw=0.0,
                         credits_charged=1, user_cost_usd=0.01), 0.14)
    assert costs.credits_charged == 7 and costs.user_cost_usd == pytest.approx(0.14)


def test_a_deposit_buys_credits_at_the_charge_rate():
    import inspect
    from modules.payments import deposit_monitor as dm
    src = inspect.getsource(dm.DepositMonitor.__init__)
    assert "PricingConfig.CREDIT_VALUE_USD" in src and "= 0.01" not in src


def test_the_402_body_reads_the_rate(monkeypatch):
    import inspect
    from api import payment_verification as pv
    assert "* 0.01" not in inspect.getsource(pv.payment_required_response)
