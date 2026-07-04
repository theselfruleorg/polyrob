"""F7 (P1-5/P2): safer per-tx default + per-venue daily caps.

The catastrophic per-tx ceiling defaulted to $1,000,000; lower it to $1,000. The
daily cap was global across all venues, so one venue could drain the whole budget
— add per-venue caps.
"""
from core.wallet.policy import PolicyGate
from core.wallet.config import load_wallet_config


def test_default_max_per_tx_lowered_to_1000():
    cfg = load_wallet_config(env={})
    assert cfg.max_per_tx_usd == 1000.0


def test_env_parses_per_venue_caps():
    cfg = load_wallet_config(env={
        "WALLET_VENUE_DAILY_CAP_POLYMARKET_USD": "5",
        "WALLET_VENUE_DAILY_CAP_HYPERLIQUID_USD": "10",
    })
    assert cfg.per_venue_daily_cap_usd == {"polymarket": 5.0, "hyperliquid": 10.0}


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
