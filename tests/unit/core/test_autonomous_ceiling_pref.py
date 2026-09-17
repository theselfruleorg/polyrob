"""The owner can raise how much runs WITHOUT being asked — bounded, from chat.

Two ceilings get confused constantly, and they are different animals:

  AGENT_WALLET_MAX_PER_TX_USD   catastrophic-loss backstop. Since 2026-09-18 an
                                owner-approved pref replaces it in either
                                direction, clamped to WALLET_DAILY_CAP_USD —
                                the daily cap is the env-only, min-merged
                                envelope, so no raise from chat moves the
                                maximum daily loss (see core/wallet/config.py).
  DEFI_AUTONOMOUS_MAX_USD       how much executes without an owner approval.
                                Raising it does NOT raise maximum loss -- the
                                backstop above still binds -- it only reduces
                                how often the owner is interrupted.

So the second is safe to expose to the owner and the first is not. 2026-09-09,
the owner: "we need to raise ceiling too can it be done internally?" -- this is
the half that can be, and it is the half that was actually causing the friction
($5 meant a $96 bridge queued for a tap).
"""
import pytest

from core import prefs
from core.wallet import tx_guard


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "120")
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "5")
    return tmp_path


def test_default_is_the_env_value(home):
    assert tx_guard.autonomous_max_usd(user_id="u", home_dir=home) == 5.0


def test_the_owner_can_raise_it(home):
    prefs.write_preference(home, "u", "budget.defi_autonomous_usd", 100.0)
    assert tx_guard.autonomous_max_usd(user_id="u", home_dir=home) == 100.0


def test_it_can_never_exceed_the_catastrophic_backstop(home):
    """The whole safety argument. A pref may reduce how often the owner is
    asked; it may never widen how much can be lost in one transaction."""
    prefs.write_preference(home, "u", "budget.defi_autonomous_usd", 5000.0)
    assert tx_guard.autonomous_max_usd(user_id="u", home_dir=home) == 120.0, (
        "the autonomous ceiling must clamp to AGENT_WALLET_MAX_PER_TX_USD")


def test_lowering_still_works(home):
    prefs.write_preference(home, "u", "budget.defi_autonomous_usd", 1.0)
    assert tx_guard.autonomous_max_usd(user_id="u", home_dir=home) == 1.0


def test_no_tenant_falls_back_to_env_not_to_a_wider_value(home):
    """A lookup that cannot identify the owner must not silently widen."""
    assert tx_guard.autonomous_max_usd(user_id=None, home_dir=None) == 5.0


def test_a_broken_pref_store_falls_back_to_env(home, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("prefs unreadable")
    monkeypatch.setattr(prefs, "resolve", _boom)
    assert tx_guard.autonomous_max_usd(user_id="u", home_dir=home) == 5.0


def test_the_legacy_call_shape_still_works(home):
    """Existing callers pass nothing; they must keep the env behaviour."""
    assert tx_guard.autonomous_max_usd() == 5.0
