"""A settled room-action invoice routes to apply, not to a self-wake (046)."""
import json
import uuid

import pytest

from modules.x402 import invoicing

TREASURY = "0x" + "11" * 20
ROB = "0x" + "bb" * 20


async def _mint_room_action(db, *, status="pending", offer_id="off_1"):
    rid = f"inv_{uuid.uuid4().hex[:12]}"
    meta = {"kind": "room_action", "tenant_id": "u1", "wake_delivered": False,
            "room_action": {"offer_id": offer_id, "surface": "telegram",
                            "chat_id": "-100123", "verb": "mute",
                            "target": "9911"}}
    await db.execute(
        """INSERT INTO x402_payment_requests(id,amount,amount_usd,asset,chain,
               recipient,nonce,deadline,status,metadata,
               asset_id,asset_address,asset_decimals,amount_raw,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (rid, "0.5", 0.5, "rob", "robinhood", TREASURY, rid, 9_999_999_999,
         status, json.dumps(meta), "rob", ROB, 18, str(5 * 10 ** 18)))
    return rid


@pytest.mark.asyncio
async def test_the_scan_matches_a_room_action_invoice(x402_db):
    """⚠️ match_pending_invoice filters on kind. Without room_action in the
    allowed set, a paid offer sits pending forever with the money received."""
    rid = await _mint_room_action(x402_db)
    assert await invoicing.match_pending_invoice(
        TREASURY, ROB, 5 * 10 ** 18, db=x402_db) is None
    hit = await invoicing.match_pending_invoice(
        TREASURY, ROB, 5 * 10 ** 18, kinds=tuple(invoicing.PAYABLE_KINDS),
        db=x402_db)
    assert hit["request_id"] == rid
    assert hit["room_action"]["offer_id"] == "off_1"


@pytest.mark.asyncio
async def test_a_settled_room_action_is_returned_for_notification(x402_db):
    """⚠️ settled_unnotified_invoices filtered on agent_invoice only. A
    producer missing from it mints rows that take real money and are then never
    notified, never actuated, and invisible to the owner."""
    rid = await _mint_room_action(x402_db, status="completed")
    rows = await invoicing.settled_unnotified_invoices(db=x402_db)
    row = next(r for r in rows if r["request_id"] == rid)
    assert row["kind"] == "room_action"
    assert row["room_action"]["offer_id"] == "off_1"


@pytest.mark.asyncio
async def test_notify_routes_a_room_action_to_apply_and_not_to_a_self_wake(
        monkeypatch):
    from modules.x402.settlement_notify import SettlementNotifyMixin
    applied, woke, notices = [], [], []

    class _W(SettlementNotifyMixin):
        _db = None
        _room_container = None
        _room_moderator = None

        async def _deliver_session_notice(self, inv, text, kind):
            woke.append(inv)
            return False

        async def _push_owner_notice(self, *a, **kw):
            notices.append((a, kw))

    class _Res:
        ok = True
        text = "✅ applied"

    async def fake_apply(container, offer_id, **kw):
        applied.append(offer_id)
        return _Res()

    monkeypatch.setattr("core.surfaces.room_actions.apply", fake_apply)
    monkeypatch.setattr("core.surfaces.room_actions.mark_settled",
                        lambda c, oid, **kw: True)

    await _W()._notify({"request_id": "inv_1", "kind": "room_action",
                        "room_action": {"offer_id": "off_1"},
                        "amount_usd": 0.5, "user_id": "u", "session_id": ""})
    assert applied == ["off_1"]
    assert woke == [], "a room action woke a session instead of acting"


@pytest.mark.asyncio
async def test_a_failed_apply_raises_an_owner_notice(monkeypatch):
    """⚠️ A credit is money we HOLD against an undelivered service — an owner
    notice, not a log line."""
    from modules.x402.settlement_notify import SettlementNotifyMixin
    notices = []

    class _W(SettlementNotifyMixin):
        _db = None
        _room_container = None
        _room_moderator = None

        async def _deliver_session_notice(self, inv, text, kind):
            return False

        async def _push_owner_notice(self, user_id, text, **kw):
            notices.append(text)

    class _Res:
        ok = False
        text = "⚠️ I took payment ... credit cr_1"

    monkeypatch.setattr("core.surfaces.room_actions.apply",
                        lambda c, oid, **kw: _async(_Res()))
    monkeypatch.setattr("core.surfaces.room_actions.mark_settled",
                        lambda c, oid, **kw: True)
    await _W()._notify({"request_id": "inv_1", "kind": "room_action",
                        "room_action": {"offer_id": "off_1"},
                        "amount_usd": 0.5, "user_id": "u", "session_id": ""})
    assert notices and "credit" in notices[0].lower()


@pytest.mark.asyncio
async def test_a_room_action_invoice_with_no_offer_id_is_reported_not_crashed(
        monkeypatch):
    from modules.x402.settlement_notify import SettlementNotifyMixin

    class _W(SettlementNotifyMixin):
        _db = None

        async def _deliver_session_notice(self, inv, text, kind):
            return False

        async def _push_owner_notice(self, *a, **kw):
            return None

    await _W()._notify({"request_id": "inv_1", "kind": "room_action",
                        "room_action": {}, "amount_usd": 0.5,
                        "user_id": "u", "session_id": ""})


@pytest.mark.asyncio
async def test_an_ordinary_invoice_still_wakes_its_session(monkeypatch):
    """Regression guard: the branch must not swallow the normal path."""
    from modules.x402.settlement_notify import SettlementNotifyMixin
    woke = []

    class _W(SettlementNotifyMixin):
        _db = None

        async def _deliver_session_notice(self, inv, text, kind):
            woke.append(kind)
            return False

        async def _push_owner_notice(self, *a, **kw):
            return None

    await _W()._notify({"request_id": "inv_1", "kind": "agent_invoice",
                        "amount_usd": 0.5, "user_id": "u",
                        "session_id": "sess_1", "purpose": "p"})
    assert woke == ["payment_settled"]


async def _async(value):
    return value
