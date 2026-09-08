"""F7 (P1-5/P2): safer per-tx default + per-venue daily caps.

The catastrophic per-tx ceiling defaulted to $1,000,000; lower it to $1,000
(H3, 2026-08-22: lowered again to $250 — $1000 in one transaction is not
"catastrophe-only" on this treasury). The daily cap was global across all
venues, so one venue could drain the whole budget — add per-venue caps.
"""
import pytest

from core.wallet.policy import PolicyGate
from core.wallet.config import load_wallet_config


def test_default_max_per_tx_is_250():
    # Minor 5 (fix round 1, 2026-08-22 review): renamed — the old name
    # ("...lowered_to_1000") lied about what this pins since H3 lowered the
    # default again, to $250. See core/wallet/config.py::DEFAULT_MAX_PER_TX_USD.
    cfg = load_wallet_config(env={})
    assert cfg.max_per_tx_usd == 250.0


def test_env_parses_per_venue_caps():
    cfg = load_wallet_config(env={
        "WALLET_VENUE_DAILY_CAP_POLYMARKET_USD": "5",
        "WALLET_VENUE_DAILY_CAP_HYPERLIQUID_USD": "10",
    })
    assert cfg.per_venue_daily_cap_usd == {"polymarket": 5.0, "hyperliquid": 10.0}


# --- Minor 2 (fix round 1, 2026-08-22 review): the per-venue cap parser used
# to silently DROP a malformed venue cap (the last silent-cap-drop left after
# the H3 daily/per-tx rewrite) instead of raising like its siblings. ---------

def test_malformed_venue_cap_raises_naming_the_key_and_value():
    with pytest.raises(ValueError) as exc:
        load_wallet_config(env={"WALLET_VENUE_DAILY_CAP_HYPERLIQUID_USD": "1O0"})
    assert "WALLET_VENUE_DAILY_CAP_HYPERLIQUID_USD" in str(exc.value)
    assert "1O0" in str(exc.value)


def test_venue_cap_explicit_sentinel_means_no_venue_cap():
    """Same disable-sentinel set as the daily/per-tx caps, for consistency —
    the effect is identical to leaving the var unset (no per-venue cap; the
    global daily cap alone still applies), but the operator can say so
    explicitly."""
    cfg = load_wallet_config(env={"WALLET_VENUE_DAILY_CAP_HYPERLIQUID_USD": "none"})
    assert cfg.per_venue_daily_cap_usd == {}


def test_venue_cap_negative_raises():
    with pytest.raises(ValueError) as exc:
        load_wallet_config(env={"WALLET_VENUE_DAILY_CAP_HYPERLIQUID_USD": "-5"})
    assert "WALLET_VENUE_DAILY_CAP_HYPERLIQUID_USD" in str(exc.value)
    assert "-5" in str(exc.value)


# --- Minor 4 (fix round 1, 2026-08-22 review): a negative cap is never
# meaningful — refuse it, naming the key and the value, on every ceiling. ---

def test_negative_daily_cap_raises_naming_the_key_and_value():
    with pytest.raises(ValueError) as exc:
        load_wallet_config(env={"WALLET_DAILY_CAP_USD": "-5",
                                "AGENT_WALLET_ENABLED": "false"})
    assert "WALLET_DAILY_CAP_USD" in str(exc.value)
    assert "-5" in str(exc.value)


def test_negative_per_tx_cap_raises_naming_the_key_and_value():
    with pytest.raises(ValueError) as exc:
        load_wallet_config(env={"AGENT_WALLET_MAX_PER_TX_USD": "-1",
                                "AGENT_WALLET_ENABLED": "false"})
    assert "AGENT_WALLET_MAX_PER_TX_USD" in str(exc.value)
    assert "-1" in str(exc.value)


def test_per_venue_cap_blocks_only_that_venue():
    t = {"t": 1_000_000.0}
    gate = PolicyGate(
        max_per_tx_usd=10_000.0,
        per_venue_daily_cap_usd={"polymarket": 5.0},
        clock=lambda: t["t"],
    )
    assert gate.check(venue="polymarket", amount_usd=5.0, idempotency_key="a").allowed
    gate.record(venue="polymarket", action="trade", amount_usd=5.0,
                counterparty=None, idempotency_key="a", result_ref=None)
    # polymarket exhausted...
    assert not gate.check(venue="polymarket", amount_usd=1.0, idempotency_key="b").allowed
    # ...but another venue is unaffected.
    assert gate.check(venue="hyperliquid", amount_usd=100.0, idempotency_key="c").allowed


def test_per_venue_cap_respects_24h_window():
    t = {"t": 1_000_000.0}
    gate = PolicyGate(
        max_per_tx_usd=10_000.0,
        per_venue_daily_cap_usd={"polymarket": 5.0},
        clock=lambda: t["t"],
    )
    gate.record(venue="polymarket", action="t", amount_usd=5.0,
                counterparty=None, idempotency_key="a", result_ref=None)
    t["t"] += 86_400 + 1  # roll past the window
    assert gate.check(venue="polymarket", amount_usd=5.0, idempotency_key="b").allowed


def test_global_cap_still_enforced_alongside_venue_caps():
    t = {"t": 1_000_000.0}
    gate = PolicyGate(
        max_per_tx_usd=10_000.0,
        daily_cap_usd=8.0,
        per_venue_daily_cap_usd={"polymarket": 100.0},
        clock=lambda: t["t"],
    )
    gate.record(venue="hyperliquid", action="t", amount_usd=8.0,
                counterparty=None, idempotency_key="a", result_ref=None)
    # Global cap hit even though polymarket's venue cap has room.
    assert not gate.check(venue="polymarket", amount_usd=1.0, idempotency_key="b").allowed
