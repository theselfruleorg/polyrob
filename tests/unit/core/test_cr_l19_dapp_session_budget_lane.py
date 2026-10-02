"""CR-L19: the tiered lane's dapp_connect exemption bounds the SESSION budget too."""
import pytest

from core.config_policy.spend_lane import spend_exemption

VERB = "dapp_browser_dapp_connect"


@pytest.fixture(autouse=True)
def _lane(monkeypatch):
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "25")


def test_small_per_tx_over_huge_session_budget_keeps_the_tap():
    assert spend_exemption(VERB, {"max_spend_usd": 1.0,
                                  "session_budget_usd": 10_000.0}) is None


def test_missing_session_budget_keeps_the_tap():
    assert spend_exemption(VERB, {"max_spend_usd": 1.0}) is None


def test_both_within_the_ceiling_is_exempt():
    assert spend_exemption(VERB, {"max_spend_usd": 1.0,
                                  "session_budget_usd": 20.0})


def test_other_verbs_unchanged():
    assert spend_exemption("defi_trade_swap", {"dry_run": False,
                                               "max_spend_usd": 1.0})
