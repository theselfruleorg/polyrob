"""Audit 2026-11-21 L-7: the dapp wallet signs an ``owner_queue`` transaction only
after the owner approved it — and then records that exact, gas-sized transaction
with ``attest.record_owner_cleared``. The rail here enforces the real rule."""
import pytest

from core.signer import attest
from tests.unit.tools.dapp_browser import test_bridge as base


@pytest.fixture
def attest_rail(monkeypatch):
    attest._reset_for_tests()
    seen = []

    def sign_and_send(self, tx):
        if attest.take(tx) is None:
            raise RuntimeError("no tx_guard authorization is recorded; nothing was sent")
        self.sent = True
        seen.append(tx)
        return "0x" + "ee" * 32
    monkeypatch.setattr(base._Rail, "sign_and_send", sign_and_send)
    yield seen
    attest._reset_for_tests()


@pytest.mark.asyncio
async def test_an_owner_approved_dapp_tx_is_recorded_and_sent(attest_rail):
    bridge, _ = base._bridge(allow=False, lane="owner_queue", approver=base._Approver(True))
    out = await base._ask(bridge, "eth_sendTransaction", [{"to": base.POOL, "data": "0x01"}])
    assert out["result"] == "0x" + "ee" * 32
    assert len(attest_rail) == 1


@pytest.mark.asyncio
async def test_an_owner_decline_records_nothing(attest_rail):
    bridge, _ = base._bridge(allow=False, lane="owner_queue", approver=base._Approver(False))
    out = await base._ask(bridge, "eth_sendTransaction", [{"to": base.POOL, "data": "0x01"}])
    assert "error" in out and attest_rail == [] and not attest._items
