import pytest
from modules.credits.unified_ledger import build_ledger


class FakeDB:
    """Minimal database_manager double: fetch_one dispatches on the SQL text."""
    def __init__(self, *, api_usd=2.47, api_total=13.97, calls=100, calls_total=561,
                 settled_usd=0.0, settled_n=0, pending_usd=2.0, pending_n=1):
        self.api_usd, self.api_total = api_usd, api_total
        self.calls, self.calls_total = calls, calls_total
        self.settled_usd, self.settled_n = settled_usd, settled_n
        self.pending_usd, self.pending_n = pending_usd, pending_n

    async def fetch_one(self, sql, params=None):
        if "usage_records" in sql:
            # windowed query filters on timestamp; total query does not
            if "timestamp >=" in sql:
                return {"api_usd": self.api_usd, "credits": 6.0, "calls": self.calls}
            return {"api_usd": self.api_total, "credits": 30.0, "calls": self.calls_total}
        if "status = 'pending'" in sql:
            return {"usd": self.pending_usd, "n": self.pending_n}
        if "x402_payment_requests" in sql:
            return {"usd": self.settled_usd, "n": self.settled_n}
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_treasury_net_excludes_runtime_cost():
    """THE regression test for the merge bug: the owner's API bill must never
    enter Rob's net. Historically net_usd = earned - (api_cost + wallet)."""
    led = await build_ledger("rob", days=1, db=FakeDB(api_usd=2.47, settled_usd=5.0))
    assert led["treasury"]["income_usd"] == 5.0
    assert led["treasury"]["spend_usd"] == 0.0          # no wallet events
    assert led["treasury"]["net_usd"] == 5.0            # income - spend, NOT minus 2.47
    assert led["runtime"]["spend_window_usd"] == 2.47
    # pending invoices (FakeDB default: $2.00 across 1 open request) were
    # computed but never asserted anywhere.
    assert led["treasury"]["pending_usd"] == 2.0
    assert led["treasury"]["pending_count"] == 1
    # healthy path: both legs read fine, both blocks report available.
    assert led["treasury"]["available"] is True
    assert led["runtime"]["available"] is True


@pytest.mark.asyncio
async def test_treasury_unavailable_when_wallet_metering_broken(monkeypatch):
    """H14b: treasury["available"] must reflect BOTH legs it depends on, not
    just the income read. spend_usd/net_usd come from _wallet_leg; if that leg
    errored (or is disabled), spend_usd is a fabricated $0.00 and net_usd is
    therefore wrong too — even though the x402 income leg read fine. A
    renderer must be able to tell "we couldn't read it" from "we read it and
    it was zero", so treasury must NOT claim available=True here."""
    import modules.credits.unified_ledger as ul

    def _broken_wallet_leg(user_id, days):
        return {"wallet_spend_usd": 0.0, "wallet_payments": 0, "wallet_metering": "error"}

    monkeypatch.setattr(ul, "_wallet_leg", _broken_wallet_leg)
    led = await build_ledger("rob", days=1, db=FakeDB(settled_usd=5.0))
    assert led["treasury"]["income_usd"] == 5.0         # income leg read fine
    assert led["treasury"]["spend_usd"] == 0.0          # fabricated zero from the broken leg
    assert led["treasury"]["available"] is False        # must NOT look healthy


@pytest.mark.asyncio
async def test_runtime_window_and_total_are_independent():
    led = await build_ledger("rob", days=1, db=FakeDB(api_usd=2.47, api_total=13.97,
                                                      calls=100, calls_total=561))
    assert led["runtime"]["spend_window_usd"] == 2.47
    assert led["runtime"]["spend_total_usd"] == 13.97
    assert led["runtime"]["calls_window"] == 100
    assert led["runtime"]["calls_total"] == 561


@pytest.mark.asyncio
async def test_balances_omitted_by_default_and_no_network_read():
    """include_balances defaults False: both balance fields are None and no probe runs."""
    led = await build_ledger("rob", days=1, db=FakeDB())
    assert led["treasury"]["balance_usd"] is None
    assert led["runtime"]["provider_balance_usd"] is None


@pytest.mark.asyncio
async def test_no_treasury_key_uses_legacy_names():
    led = await build_ledger("rob", days=1, db=FakeDB())
    assert "earned_usd" not in led["treasury"]
    assert "spent_usd" not in led["treasury"]
    assert "spent_window_usd" not in led["runtime"]


@pytest.mark.asyncio
async def test_merged_fields_are_gone():
    """No alias: a surviving total_spend_usd would let a consumer silently keep
    reading the merge. Deleting makes any straggler fail loudly."""
    led = await build_ledger("rob", days=1, db=FakeDB())
    assert "total_spend_usd" not in led
    assert "net_usd" not in led            # top level; treasury.net_usd remains
    assert "earned_usd" not in led
    assert "income_usd" not in led         # top level; treasury.income_usd remains
    assert led["treasury"]["net_usd"] == 0.0
