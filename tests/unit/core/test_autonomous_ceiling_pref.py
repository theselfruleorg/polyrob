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


# --- M09 (security analysis 2026-09-23): non-finite values -------------------

@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", float("nan"), float("inf")])
def test_a_non_finite_pref_is_refused(raw):
    ok, _val, err = prefs.validate_pref("budget.defi_autonomous_usd", raw)
    assert not ok and "finite" in err
    ok, _val, _err = prefs.validate_pref("budget.wallet_daily_usd", raw)
    assert not ok


def test_a_nan_ceiling_falls_back_to_the_env_value(home, monkeypatch):
    """`amount > NaN` is always False: a NaN ceiling waved every spend through."""
    monkeypatch.setattr(prefs, "resolve", lambda *a, **kw: float("nan"))
    got = tx_guard.autonomous_max_usd(user_id="u", home_dir=home)
    assert got == 5.0


def test_a_hand_edited_nan_in_the_file_never_reaches_the_guard(home):
    import math
    from pathlib import Path
    prefs.write_preference(home, "u", "budget.defi_autonomous_usd", 100.0)
    files = [p for p in Path(home).rglob("*.toml")]
    assert files, "preference file not found"
    for f in files:
        f.write_text(f.read_text().replace("100.0", "nan"))
    got = tx_guard.autonomous_max_usd(user_id="u", home_dir=home)
    assert math.isfinite(got) and got == 5.0
