"""057 WS-E — the `message` tool's result carries its rail's PROOF rule.

Prod 2026-09-19: a channel post succeeded, the agent then tried to "confirm" it
by reading the room, found nothing (Telegram never echoes a bot's own channel
posts back) and reported a delivered post as unconfirmed. The proof rule now
travels with the receipt instead of being looked up afterwards — and it is
rendered from the ONE table, not restated here.
"""
import asyncio
import os
import tempfile

from core.rails.verification import verification_line
from core.surfaces.outbound_allowlist import OutboundAllowlist
from tools.controller.message_send import perform_message_send
from tools.controller.turn_origin import _message_action_result


class _Router:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((surface_id, chat_id, text))
        return self.ok

    def capabilities(self, surface_id):
        from core.surfaces.envelopes import SurfaceCapabilities
        return SurfaceCapabilities(media_out=False)


def _send(ok=True, surface="telegram", target="999"):
    tmp = tempfile.mkdtemp()
    return asyncio.run(perform_message_send(
        router=_Router(ok), allowlist=OutboundAllowlist(os.path.join(tmp, "a.db")),
        owner_targets={"telegram": "999", "email": "o@example.com"},
        user_id="rob", surface=surface, target=target, text="hi", action="send"))


def test_successful_telegram_send_carries_the_channel_proof_rule():
    res = _send()
    assert res["success"] is True
    assert res["verification"] == verification_line("telegram_channel")
    assert "cannot read its own channel posts" in res["verification"]


def test_email_send_carries_the_email_rule():
    res = _send(surface="email", target="o@example.com")
    assert res["success"] is True
    assert res["verification"] == verification_line("email")


def test_a_failed_send_claims_no_proof():
    res = _send(ok=False)
    assert res["success"] is False
    assert "verification" not in res


def test_the_action_result_renders_the_line():
    res = _send()
    out = _message_action_result(res, "telegram", "999", "hi").extracted_content
    assert out.splitlines()[-1] == res["verification"]


def test_a_rail_with_no_row_adds_nothing_rather_than_a_made_up_rule():
    res = {"success": True, "tier": "owner", "surface": "whatsapp",
           "target": "1", "error": None}
    out = _message_action_result(res, "whatsapp", "1", "hi").extracted_content
    assert "proof:" not in out
