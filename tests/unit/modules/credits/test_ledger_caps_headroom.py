"""A6 (043 A39): `build_ledger`'s `caps` block carries wallet PolicyGate
headroom — `daily_cap_usd`/`daily_used_usd`/`daily_left_usd`/`per_tx_cap_usd`
— so a finance surface can say how much of today's cap is LEFT, not just
whether metering is on. Caps are configuration, never money: this block is
never folded into `treasury`.

`_policy_gate()` is the module-level seam over
`core.wallet.factory.get_policy_gate()`, fail-open to ``None`` (disabled /
misconfigured wallet) so every cap value degrades to ``None`` — never a
fabricated ``0`` (a $0.00 cap would read as "spending is fully blocked",
which is not what "we couldn't check" means).
"""
import asyncio

import modules.credits.unified_ledger as ul
from modules.credits.unified_ledger import build_ledger


class _FakeDB:
    """Minimal database_manager double — every money leg reads a clean zero so
    this test is only about the `caps` block, not the money legs."""

    async def fetch_one(self, sql, params=None):
        if "usage_records" in sql:
            return {"api_usd": 0.0, "credits": 0.0, "calls": 0}
        if "status = 'pending'" in sql:
            return {"usd": 0.0, "n": 0}
        if "x402_payment_requests" in sql:
            return {"usd": 0.0, "n": 0}
        raise AssertionError(f"unexpected SQL: {sql}")


def test_ledger_caps_headroom(monkeypatch):
    class G:  # fake gate
        daily_cap_usd = 100.0

        def rolling_24h_spend_usd(self):
            return 20.0

    monkeypatch.setattr(ul, "_policy_gate", lambda: G())
    led = asyncio.run(build_ledger("u1", days=7, db=_FakeDB()))
    assert led["caps"]["daily_left_usd"] == 80.0
    assert led["caps"]["daily_cap_usd"] == 100.0
    assert led["caps"]["daily_used_usd"] == 20.0


def test_ledger_caps_per_tx_cap_carried_through(monkeypatch):
    class G:
        daily_cap_usd = 100.0
        per_tx_cap_usd = 25.0

        def rolling_24h_spend_usd(self):
            return 20.0

    monkeypatch.setattr(ul, "_policy_gate", lambda: G())
    led = asyncio.run(build_ledger("u1", days=7, db=_FakeDB()))
    assert led["caps"]["per_tx_cap_usd"] == 25.0


def test_ledger_caps_all_none_when_gate_unreadable(monkeypatch):
    """The gate is unreadable (disabled wallet, misconfig, ...) — every cap
    value is None, never a fabricated 0 (H14b discipline applied to caps)."""
    monkeypatch.setattr(ul, "_policy_gate", lambda: None)
    led = asyncio.run(build_ledger("u1", days=7, db=_FakeDB()))
    assert led["caps"] == {
        "daily_cap_usd": None, "daily_used_usd": None,
        "daily_left_usd": None, "per_tx_cap_usd": None,
    }


def test_ledger_caps_none_daily_cap_leaves_left_none(monkeypatch):
    """An operator-disabled cap (real PolicyGate, daily_cap_usd=None) must not
    fabricate a $0.00 headroom — `daily_left_usd` stays None too, even though
    the gate itself is perfectly readable."""
    from core.wallet.policy import PolicyGate

    gate = PolicyGate(max_per_tx_usd=25.0, daily_cap_usd=None)
    monkeypatch.setattr(ul, "_policy_gate", lambda: gate)
    led = asyncio.run(build_ledger("u1", days=7, db=_FakeDB()))
    assert led["caps"]["daily_cap_usd"] is None
    assert led["caps"]["daily_left_usd"] is None
    # per_tx_cap_usd is independent of the daily cap and still reads through.
    assert led["caps"]["per_tx_cap_usd"] == 25.0


def test_policy_gate_seam_fails_open_on_exception(monkeypatch):
    """`_policy_gate()` must never raise — a broken factory (disabled wallet,
    bad env) degrades to None, not an exception that would break the ledger."""
    import core.wallet.factory as factory

    def _boom():
        raise RuntimeError("wallet misconfigured")

    monkeypatch.setattr(factory, "get_policy_gate", _boom)
    assert ul._policy_gate() is None
