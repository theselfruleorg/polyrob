"""CR-M07: build_ledger never hands a non-owner tenant the operator wallet's
balance, the provider credit, or the spend-cap headroom."""
import pytest

import modules.credits.balances as B
import modules.credits.unified_ledger as ul
from modules.credits.unified_ledger import build_ledger
from tests.unit.modules.credits.test_unified_ledger_split import FakeDB


class _Gate:
    daily_cap_usd = 100.0
    per_tx_cap_usd = 25.0

    def rolling_24h_spend_usd(self):
        return 20.0


@pytest.fixture
def _probes(monkeypatch):
    calls = []

    async def fake_treasury(user_id):
        calls.append("treasury")
        return 12.34

    async def fake_provider():
        calls.append("provider")
        return 5.67

    monkeypatch.setattr(B, "treasury_balance_usd", fake_treasury)
    monkeypatch.setattr(B, "provider_balance_usd", fake_provider)
    monkeypatch.setattr(ul, "_policy_gate", lambda: _Gate())
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    return calls


@pytest.mark.asyncio
async def test_non_owner_sees_no_operator_facts(_probes):
    led = await build_ledger("u_stranger", days=1, db=FakeDB(), include_balances=True)
    assert led["treasury"]["balance_usd"] is None
    assert led["runtime"]["provider_balance_usd"] is None
    assert set(led["caps"].values()) == {None}
    assert _probes == []  # the operator wallet was never even read


@pytest.mark.asyncio
async def test_owner_still_sees_them(_probes):
    led = await build_ledger("owner-1", days=1, db=FakeDB(), include_balances=True)
    assert led["treasury"]["balance_usd"] == 12.34
    assert led["runtime"]["provider_balance_usd"] == 5.67
    assert led["caps"]["daily_left_usd"] == 80.0
