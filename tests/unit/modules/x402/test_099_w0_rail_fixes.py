"""099 W0: the receivables-rail defects fixed before any offer exists.

F1 — a DROPPED self-wake is told to the owner, never reported as "the agent is on it".
F2 — the x402 `/pay` challenge asks for the row's stored `amount_raw`, not the round dollars.
F5 — a room-action settlement emits `payment_settled` ONCE.
F6 — a correspondent delivery that failed does not count as delivered (no 8004 offer).
"""
import pytest

from modules.x402.settlement_notify import SettlementNotifyMixin


class _Agent:
    def __init__(self, wake_ok=True, corr_ok=True):
        self.wake_ok, self.corr_ok = wake_ok, corr_ok

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        return self.wake_ok

    async def deliver_correspondent_data(self, session_id, source, text, metadata=None):
        return self.corr_ok


def _watcher(agent, *, corr_active=False):
    class _W(SettlementNotifyMixin):
        _db = None

        def __init__(self):
            self.task_agent = agent
            self.owner_notices = []

        def _correspondent_active(self, cref):
            return corr_active

        async def _push_owner_notice(self, user_id, text, source="", session_id=None):
            self.owner_notices.append(text)

    return _W()


_INV = {"request_id": "inv_1", "kind": "agent_invoice", "amount_usd": 5.0,
        "user_id": "u", "session_id": "sess_1", "purpose": "p",
        "chain": "base", "asset_id": "usdc-base"}


@pytest.fixture(autouse=True)
def _income_chain(monkeypatch):
    monkeypatch.setattr("modules.x402.income_chains.is_income_chain", lambda *a, **k: True)


@pytest.mark.asyncio
async def test_f1_dropped_wake_is_named_in_the_owner_notice():
    w = _watcher(_Agent(wake_ok=False))
    await w._notify(dict(_INV))
    assert len(w.owner_notices) == 1
    assert "could not be resumed" in w.owner_notices[0]


@pytest.mark.asyncio
async def test_f1_a_delivered_wake_keeps_the_plain_notice():
    w = _watcher(_Agent(wake_ok=True))
    await w._notify(dict(_INV))
    assert "could not be resumed" not in w.owner_notices[0]


@pytest.mark.asyncio
async def test_f6_failed_correspondent_delivery_is_named():
    inv = dict(_INV, correspondent_ref={"surface": "email", "address": "x@y.z"})
    w = _watcher(_Agent(corr_ok=False), corr_active=True)
    assert await w._deliver_session_notice(inv, "t", "payment_settled") == "correspondent_dropped"


@pytest.mark.asyncio
async def test_f5_room_action_emits_payment_settled_once(monkeypatch):
    events = []
    monkeypatch.setattr("modules.x402.invoicing._emit",
                        lambda name, **kw: events.append(name))
    w = _watcher(_Agent())

    async def _apply(inv):
        from modules.x402.invoicing import _emit
        _emit("payment_settled", user_id="u", session_id="", attrs={})
    w._apply_room_action = _apply
    await w._notify(dict(_INV, kind="room_action", room_action={"offer_id": "o1"}))
    assert events == ["payment_settled"]


def test_f2_challenge_uses_the_stored_raw_amount():
    from api.x402_endpoints import _invoice_atomic
    # a pinned-raw row: round dollars, a raw tail the settlement match compares
    assert _invoice_atomic({"amount_usd": 0.5, "amount_raw": "500321"}, 6) == 500321
    # a legacy row with no raw derives it from the dollars
    assert _invoice_atomic({"amount_usd": 0.5, "amount_raw": None}, 6) == 500000
