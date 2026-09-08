"""B10 (S6, 2026-08-29): both settlement outcomes reach the OWNER.

``_notify_expired`` always pushed an owner notice over the durable delivery
rail; ``_notify`` (settlement) only re-entered the session, so when the session
was not wakeable the owner never heard that money had ARRIVED. Both now ride
one session-notice helper and one unconditional owner notice.
"""
import asyncio

import pytest

import core.surfaces.user_delivery as _ud
from modules.x402.settlement_watcher import SettlementWatcher


class _Agent:
    container = object()

    def __init__(self, wake_ok: bool):
        self.wake_ok = wake_ok
        self.wakes = []

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return self.wake_ok


@pytest.fixture
def owner_notices(monkeypatch):
    sent = []

    async def fake_deliver(container, user_id, text, **kw):
        sent.append((user_id, text, kw))
        return True

    monkeypatch.setattr(_ud, "deliver_user_message", fake_deliver)
    monkeypatch.setattr("modules.x402.invoicing._emit", lambda *a, **k: None)
    return sent


def _inv(**over):
    base = {"request_id": "req-1", "user_id": "u1", "session_id": "s1",
            "amount_usd": 12.5, "purpose": "audit", "transaction_hash": "0xabc"}
    base.update(over)
    return base


def test_settlement_pushes_owner_notice_even_when_session_not_wakeable(owner_notices):
    agent = _Agent(wake_ok=False)
    w = SettlementWatcher(agent, db=None)
    asyncio.run(w._notify(_inv()))
    assert len(agent.wakes) == 1                       # session rail still tried
    assert len(owner_notices) == 1                     # and the owner heard anyway
    user_id, text, kw = owner_notices[0]
    assert user_id == "u1" and "req-1" in text and "12.50" in text
    assert kw.get("source") == "x402_invoice"


def test_settlement_and_expiry_share_the_rails(owner_notices):
    agent = _Agent(wake_ok=True)
    w = SettlementWatcher(agent, db=None)
    asyncio.run(w._notify(_inv()))
    asyncio.run(w._notify_expired(_inv(request_id="req-2")))
    kinds = [m.get("kind_hint") for _, _, _, m in agent.wakes]
    assert kinds == ["payment_settled", "payment_expired"]
    assert [n[0] for n in owner_notices] == ["u1", "u1"]
    assert "SETTLED" in owner_notices[0][1].upper() and "expired" in owner_notices[1][1]
