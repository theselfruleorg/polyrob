"""The per-payment cap shown is never above the daily cap that binds it.

The gate checks ``spent + amount`` against the daily cap, so one payment can
never move more than the daily cap; the built-in per-tx default (250) above the
built-in daily default (100) displayed a number nobody could spend. Display only:
``configured_max_per_tx_usd`` (enforcement) is unchanged.
"""
from core import prefs
from core.wallet.config import configured_max_per_tx_usd


def _clean(monkeypatch):
    for k in ("AGENT_WALLET_MAX_PER_TX_USD", "WALLET_DAILY_CAP_USD"):
        monkeypatch.delenv(k, raising=False)


def test_the_default_per_tx_shows_the_daily_cap(monkeypatch, tmp_path):
    _clean(monkeypatch)
    value, source = prefs.display_effective("budget.wallet_per_tx_usd", "u1", tmp_path, "i1")
    assert value == 100 and "daily cap $100 binds one payment first" in source
    assert source.startswith("built-in")
    # enforcement is not changed by the display
    assert configured_max_per_tx_usd("u1", tmp_path) == 250.0


def test_a_wider_daily_cap_shows_the_per_tx_value(monkeypatch, tmp_path):
    _clean(monkeypatch)
    monkeypatch.setenv("WALLET_DAILY_CAP_USD", "1000")
    value, source = prefs.display_effective("budget.wallet_per_tx_usd", "u1", tmp_path, "i1")
    assert value == 250 and "binds" not in source
