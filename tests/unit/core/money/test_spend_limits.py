"""Review 2026-10-13 C-01/B5: ONE live "what may I spend now" read.

The block reads the guard's own objects (the process spend ledger, tx_guard's
autonomous ceiling) — so a cap the guard enforces is the cap the agent sees,
and an identity-doc figure ("$10/tx") cannot win.
"""
from core.money import hooks
from core.money import limits
from core.money.ledger import SpendLedger


def _register(monkeypatch, ledger):
    monkeypatch.setitem(hooks._PROVIDERS, hooks._SPEND_LEDGER, lambda: ledger)


def _ceiling(monkeypatch, value, lane=True):
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true" if lane else "false")
    from core.wallet import tx_guard
    monkeypatch.setattr(tx_guard, "autonomous_max_usd", lambda *a, **k: value)
    monkeypatch.setattr(tx_guard, "ceiling_scope", lambda ctx=None: (None, None))


def test_the_block_shows_the_numbers_the_guard_enforces(monkeypatch):
    ledger = SpendLedger(max_per_tx_usd=200.0, daily_cap_usd=3000.0)
    _register(monkeypatch, ledger)
    _ceiling(monkeypatch, 300.0)
    lines = limits.spend_limits_lines()
    text = "\n".join(lines)
    assert "OVERRIDE any spend figure in identity docs" in text
    assert "per-transaction cap: $200.00" in text
    assert "rolling 24h cap: $3,000.00 (used $0.00, left $3,000.00)" in text
    assert "no-ask ceiling: $300.00" in text
    assert "unchecked route: $5.00" in text
    # The same answer the guard gives: a $329 spend is refused by THIS cap.
    decision = ledger.check(venue="defi", amount_usd=329.42, idempotency_key=None)
    assert decision.allowed is False and "$200.00" in decision.reason


def test_a_live_cap_change_is_read_not_remembered(monkeypatch):
    caps = {"per_tx": 200.0}
    ledger = SpendLedger(max_per_tx_usd=200.0, daily_cap_usd=3000.0,
                         cap_resolver=lambda: (caps["per_tx"], 3000.0))
    _register(monkeypatch, ledger)
    _ceiling(monkeypatch, 25.0)
    caps["per_tx"] = 450.0
    assert "per-transaction cap: $450.00" in "\n".join(limits.spend_limits_lines())


def test_an_unread_leg_is_never_a_number(monkeypatch):
    def _boom():
        raise RuntimeError("no wallet")
    monkeypatch.setitem(hooks._PROVIDERS, hooks._SPEND_LEDGER, _boom)
    from core.config_policy import spend_lane

    def _raise(*a, **k):
        raise OSError("prefs")
    monkeypatch.setattr(spend_lane, "autonomous_ceiling_usd", _raise)
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
    text = "\n".join(limits.spend_limits_lines())
    assert "per-transaction cap: unreadable" in text
    assert "rolling 24h cap: unreadable" in text
    assert "no-ask ceiling: unreadable" in text


def test_a_disabled_daily_cap_says_none(monkeypatch):
    _register(monkeypatch, SpendLedger(max_per_tx_usd=100.0, daily_cap_usd=None))
    _ceiling(monkeypatch, 25.0)
    assert "rolling 24h cap: none" in "\n".join(limits.spend_limits_lines())


def test_the_unchecked_ticket_has_one_copy():
    from tools.defi import identity_gate
    assert identity_gate.UNVERIFIED_UNCHECKED_MAX_USD is limits.UNVERIFIED_UNCHECKED_MAX_USD


def test_live_health_carries_the_block_only_with_a_wallet(monkeypatch, tmp_path):
    from agents.task.agent.core import live_health
    _register(monkeypatch, SpendLedger(max_per_tx_usd=200.0, daily_cap_usd=3000.0))
    _ceiling(monkeypatch, 300.0)
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "false")
    note = live_health.build_live_health_text("owner", data_dir=str(tmp_path))
    assert "Spend limits now" not in note
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    note = live_health.build_live_health_text("owner", data_dir=str(tmp_path))
    assert "Spend limits now" in note
    assert "per-transaction cap: $200.00" in note


def test_with_the_tiered_lane_off_no_ceiling_is_claimed(monkeypatch):
    """The approval hook exempts a within-ceiling spend ONLY under the tiered lane
    (spend_lane.spend_exemption); off, every live spend asks — the block must not
    tell the agent that a spend under the ceiling runs without the owner."""
    from core.config_policy import spend_lane
    _register(monkeypatch, SpendLedger(max_per_tx_usd=200.0, daily_cap_usd=3000.0))
    _ceiling(monkeypatch, 300.0, lane=False)
    text = "\n".join(limits.spend_limits_lines())
    assert "no-ask ceiling: none in effect" in text
    assert "$300.00 per transaction" not in text
    assert spend_lane.spend_exemption("defi_trade_swap",
                                      {"dry_run": False, "max_spend_usd": 1.0}) is None
