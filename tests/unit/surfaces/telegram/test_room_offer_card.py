"""046 T12: the offer carries a payment card, and the room gets the picture.

⚠️ No room path rendered a QR at all. `render_invoice_card` had exactly ONE
caller (`tools/x402/invoice_tool.py`), the room seat returned a bare string, and
a command reply was text-only — so the payer, a stranger in a chat window, got a
treasury address to retype by hand.
"""
import os

import asyncio as _aio
import pytest

from core.surfaces.command_reply import CommandReply, reply_media
from surfaces.telegram import group_ops

_INVOICE = {
    "request_id": "inv_1", "amount_usd": 0.5, "asset": "usdc",
    "asset_symbol": "USDC", "chain": "base", "recipient": "0x" + "11" * 20,
    "purpose": "a paid mute in telegram:-100123",
    "expires_at_epoch": 4_102_444_800, "amount_raw": "500000",
}


class _Container:
    def __init__(self, tmp_path):
        self.config = type("Cfg", (), {"data_dir": str(tmp_path)})()

    def get_service(self, n):
        return None


def test_the_card_is_rendered_under_the_data_home(tmp_path, monkeypatch):
    """⚠️ NOT a session workspace: a room session is PUBLIC and garbage
    collected, and the payer needs the picture to outlive it."""
    monkeypatch.setenv("ROOM_ACTION_CARD_ENABLED", "true")
    media = group_ops._offer_card(_Container(tmp_path), "off_1", _INVOICE)
    assert media and media[0]["kind"] == "image"
    path = media[0]["path"]
    assert os.path.isfile(path) and os.path.getsize(path) > 0
    assert str(tmp_path) in path and "off_1" in path


def test_the_card_is_off_when_the_flag_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOM_ACTION_CARD_ENABLED", "false")
    assert group_ops._offer_card(_Container(tmp_path), "off_1", _INVOICE) == []


def test_a_render_fault_costs_the_picture_never_the_offer(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOM_ACTION_CARD_ENABLED", "true")

    def boom(*a, **kw):
        raise RuntimeError("no font")

    monkeypatch.setattr("modules.pfp.cards.render_invoice_card", boom)
    assert group_ops._offer_card(_Container(tmp_path), "off_1", _INVOICE) == []


def test_the_card_rides_out_on_the_offer_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOM_ACTION_CARD_ENABLED", "true")
    from core.surfaces.room_actions import OfferResult
    monkeypatch.setattr(
        "core.surfaces.room_actions.offer",
        lambda c, **kw: OfferResult(True, "💰 offer", offer_id="off_9",
                                    invoice=dict(_INVOICE)))
    out = _aio.run(group_ops._paid_offer(
        type("A", (), {"container": _Container(tmp_path)})(),
        _Container(tmp_path), "telegram", "-100123", "mute", "9911", "S",
        "2277", "1h"))
    assert isinstance(out, CommandReply) and out.to_room
    assert reply_media(out), "the offer reply carried no card"


def test_the_card_default_follows_room_actions_being_on(monkeypatch):
    """⚠️ Deliberately NOT `INVOICE_CARD_ENABLED`, which defaults to local mode
    and is therefore OFF on every server — the only posture that actually bills
    a stranger."""
    from core.config_policy import capability_toggles as ct
    monkeypatch.delenv("ROOM_ACTION_CARD_ENABLED", raising=False)
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "true")
    assert ct.room_action_card_enabled() is True
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "false")
    assert ct.room_action_card_enabled() is False


# --- the rail check names BOTH flags ----------------------------------------

def test_the_rail_check_refuses_without_onchain_detection(monkeypatch):
    """⚠️ TWO flags. `X402_INVOICE_ENABLED` starts the settlement watcher;
    `X402_SETTLE_ONCHAIN_DETECT` is what makes its tick actually scan. A room
    action has no payer who can attest and no facilitator lane, so without
    detection the money arrives and nothing ever notices."""
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "false")
    asset = type("A", (), {"chain": "base"})()
    reason = group_ops._rail_check_fn()(asset)
    assert reason and "X402_SETTLE_ONCHAIN_DETECT" in reason


def test_the_rail_check_refuses_a_chain_nothing_watches(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    asset = type("A", (), {"chain": "a-chain-that-does-not-exist"})()
    reason = group_ops._rail_check_fn()(asset)
    assert reason and "cannot watch" in reason


def test_a_ready_rail_returns_no_reason(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    asset = type("A", (), {"chain": "base"})()
    assert group_ops._rail_check_fn()(asset) is None
