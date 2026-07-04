from core.wallet.policy import PolicyGate


def test_allows_under_ceiling():
    gate = PolicyGate(max_per_tx_usd=100.0)
    d = gate.check(venue="x402", amount_usd=5.0, idempotency_key="k1")
    assert d.allowed is True and d.reason is None


def test_no_daily_cap_allows_large_cumulative():
    """Default (no daily cap) is byte-identical: many txns never trip a rolling cap."""
    gate = PolicyGate(max_per_tx_usd=100.0)
    for i in range(20):
        assert gate.check(venue="x402", amount_usd=10.0, idempotency_key=f"k{i}").allowed
        gate.record(venue="x402", action="pay", amount_usd=10.0, counterparty="0x",
                    idempotency_key=f"k{i}", result_ref="0x")


def test_daily_cap_blocks_cumulative_over_window():
    clock = {"t": 1_000_000.0}
    gate = PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=5.0, clock=lambda: clock["t"])
    # two $2 txns within the window pass (4 <= 5)
    assert gate.check(venue="x402", amount_usd=2.0, idempotency_key="a").allowed
    gate.record(venue="x402", action="pay", amount_usd=2.0, counterparty="0x",
                idempotency_key="a", result_ref="0x")
    assert gate.check(venue="x402", amount_usd=2.0, idempotency_key="b").allowed
    gate.record(venue="x402", action="pay", amount_usd=2.0, counterparty="0x",
                idempotency_key="b", result_ref="0x")
    # the third $2 (cumulative $6 > $5) is denied
    d = gate.check(venue="x402", amount_usd=2.0, idempotency_key="c")
    assert d.allowed is False and "daily" in d.reason.lower()


def test_daily_cap_ignores_entries_outside_window():
    clock = {"t": 1_000_000.0}
    gate = PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=5.0, clock=lambda: clock["t"])
    gate.record(venue="x402", action="pay", amount_usd=4.0, counterparty="0x",
                idempotency_key="old", result_ref="0x")
    # advance > 24h so the old entry no longer counts
    clock["t"] += 86_400 + 10
    d = gate.check(venue="x402", amount_usd=4.0, idempotency_key="new")
    assert d.allowed is True


def test_persistent_sink_round_trips_lifetime_spend(tmp_path):
    from core.wallet.audit_sink import JsonlAuditSink
    path = str(tmp_path / "wallet" / "audit.jsonl")
    sink = JsonlAuditSink(path)
    gate = PolicyGate(max_per_tx_usd=100.0, audit_sink=sink)
    gate.record(venue="x402", action="pay", amount_usd=3.0, counterparty="0x",
                idempotency_key="p1", result_ref="0xtx")
    # a fresh sink reads the file back; a fresh gate sees prior spend
    sink2 = JsonlAuditSink(path)
    assert len(sink2) == 1 and sink2[0]["amount_usd"] == 3.0
    gate2 = PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=5.0, audit_sink=sink2,
                       clock=lambda: sink2[0]["ts"] + 1)
    assert gate2.audit_log[0]["amount_usd"] == 3.0
    # rolling cap counts the persisted spend: $3 + $3 > $5 → denied
    assert gate2.check(venue="x402", amount_usd=3.0, idempotency_key="p2").allowed is False


def test_rejects_over_ceiling():
    gate = PolicyGate(max_per_tx_usd=10.0)
    d = gate.check(venue="x402", amount_usd=50.0, idempotency_key="k2")
    assert d.allowed is False and "ceiling" in d.reason.lower()


def test_rejects_duplicate_idempotency_key():
    gate = PolicyGate(max_per_tx_usd=100.0)
    assert gate.check(venue="x402", amount_usd=1.0, idempotency_key="dup").allowed is True
    gate.record(venue="x402", action="pay", amount_usd=1.0, counterparty="0xabc",
                idempotency_key="dup", result_ref="0xtx")
    d = gate.check(venue="x402", amount_usd=1.0, idempotency_key="dup")
    assert d.allowed is False and "idempotency" in d.reason.lower()


def test_audit_sink_records_entry_without_key_material():
    sink = []
    gate = PolicyGate(max_per_tx_usd=100.0, audit_sink=sink)
    gate.record(venue="hyperliquid", action="order", amount_usd=3.0, counterparty="ETH",
                idempotency_key="o1", result_ref="0xhash")
    assert len(sink) == 1
    entry = sink[0]
    assert entry["venue"] == "hyperliquid" and entry["amount_usd"] == 3.0
    assert "private_key" not in entry and "secret" not in entry
