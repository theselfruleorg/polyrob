"""The rolling-24h spend cap must hold ACROSS processes, not just within one.

`JsonlAuditSink` reloaded the ledger once, at construction, so a second
wallet-touching process (the owner running `polyrob wallet`/a CLI trade while the
daemon trades, or any future second worker) computed its rolling spend from a
snapshot taken at ITS start and never saw the other process's later spends. Both
could pass a nearly-exhausted `WALLET_DAILY_CAP_USD` and the aggregate blew past
it. The per-instance `reserve()` lock closes the in-process race only, and its own
docstring called the cross-process ledger out as unsolved.

The same reload gap applies to the idempotency replay guard, which is rebuilt from
the same file.
"""
import time

from core.wallet.audit_sink import JsonlAuditSink
from core.wallet.policy import PolicyGate


def _gate(path, cap=100.0, clock=None):
    return PolicyGate(
        max_per_tx_usd=1000.0,
        audit_sink=JsonlAuditSink(str(path)),
        daily_cap_usd=cap,
        clock=clock or time.time,
    )


class TestCrossProcessDailyCap:
    def test_spend_by_another_process_counts_against_the_cap(self, tmp_path):
        ledger = tmp_path / "audit.jsonl"
        daemon = _gate(ledger)                     # process A, started first
        cli = _gate(ledger)                        # process B, same ledger

        daemon.record(venue="uniswap", action="swap", amount_usd=80.0,
                      counterparty=None, idempotency_key="a1", result_ref=None)

        # B started before A's spend, so without a refresh it still sees $0 spent.
        decision = cli.check(venue="uniswap", amount_usd=50.0, idempotency_key="b1")
        assert decision.allowed is False
        assert "daily spend cap" in (decision.reason or "")

    def test_the_remaining_headroom_is_still_spendable(self, tmp_path):
        ledger = tmp_path / "audit.jsonl"
        daemon = _gate(ledger)
        cli = _gate(ledger)
        daemon.record(venue="uniswap", action="swap", amount_usd=80.0,
                      counterparty=None, idempotency_key="a1", result_ref=None)

        assert cli.check(venue="uniswap", amount_usd=15.0,
                         idempotency_key="b1").allowed is True

    def test_a_replay_key_used_by_another_process_is_blocked(self, tmp_path):
        ledger = tmp_path / "audit.jsonl"
        daemon = _gate(ledger)
        cli = _gate(ledger)
        daemon.record(venue="x402", action="pay", amount_usd=1.0,
                      counterparty="0xabc", idempotency_key="invoice-42",
                      result_ref=None)

        decision = cli.check(venue="x402", amount_usd=1.0,
                             idempotency_key="invoice-42")
        assert decision.allowed is False
        assert "replay" in (decision.reason or "")

    def test_a_processs_own_spend_is_never_double_counted(self, tmp_path):
        ledger = tmp_path / "audit.jsonl"
        gate = _gate(ledger)
        for i in range(3):
            gate.record(venue="uniswap", action="swap", amount_usd=10.0,
                        counterparty=None, idempotency_key=f"k{i}", result_ref=None)
        # $30 spent, $70 headroom — a double-counted own write would refuse this.
        assert gate.check(venue="uniswap", amount_usd=70.0,
                          idempotency_key="k9").allowed is True
        assert len(gate.audit_log) == 3

    def test_entries_outside_the_24h_window_do_not_count(self, tmp_path):
        ledger = tmp_path / "audit.jsonl"
        now = [1_000_000.0]
        daemon = _gate(ledger, clock=lambda: now[0])
        daemon.record(venue="uniswap", action="swap", amount_usd=90.0,
                      counterparty=None, idempotency_key="old", result_ref=None)
        now[0] += 86_400 + 60                      # the spend ages out
        cli = _gate(ledger, clock=lambda: now[0])
        assert cli.check(venue="uniswap", amount_usd=90.0,
                         idempotency_key="new").allowed is True

    def test_a_plain_list_sink_still_works(self, tmp_path):
        # The default (no durable sink) path must be untouched.
        gate = PolicyGate(max_per_tx_usd=1000.0, daily_cap_usd=100.0)
        gate.record(venue="uniswap", action="swap", amount_usd=80.0,
                    counterparty=None, idempotency_key="a", result_ref=None)
        assert gate.check(venue="uniswap", amount_usd=50.0,
                          idempotency_key="b").allowed is False
        assert gate.check(venue="uniswap", amount_usd=10.0,
                          idempotency_key="c").allowed is True

    def test_an_unreadable_ledger_never_blocks_the_gate(self, tmp_path, monkeypatch):
        ledger = tmp_path / "audit.jsonl"
        gate = _gate(ledger)

        def boom(*a, **kw):
            raise OSError("disk gone")

        monkeypatch.setattr(gate._audit, "refresh", boom)
        # Fail-open on the REFRESH: the in-memory view still decides (the sink's
        # own tamper warning is the durable-loss signal).
        assert gate.check(venue="uniswap", amount_usd=1.0,
                          idempotency_key="z").allowed is True


class TestSinkRefresh:
    def test_refresh_picks_up_lines_appended_by_another_writer(self, tmp_path):
        ledger = tmp_path / "audit.jsonl"
        a = JsonlAuditSink(str(ledger))
        b = JsonlAuditSink(str(ledger))
        a.append({"ts": 1.0, "venue": "v", "amount_usd": 5.0})

        assert len(b) == 0
        assert b.refresh() == 1
        assert len(b) == 1 and b[0]["amount_usd"] == 5.0

    def test_refresh_is_idempotent(self, tmp_path):
        ledger = tmp_path / "audit.jsonl"
        a = JsonlAuditSink(str(ledger))
        b = JsonlAuditSink(str(ledger))
        a.append({"ts": 1.0, "venue": "v", "amount_usd": 5.0})
        b.refresh()
        assert b.refresh() == 0
        assert len(b) == 1

    def test_refresh_on_a_missing_file_is_a_no_op(self, tmp_path):
        sink = JsonlAuditSink(str(tmp_path / "nope.jsonl"))
        assert sink.refresh() == 0
