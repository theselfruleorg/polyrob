"""2026-07-19 fix: a goal-completion `message` action targeted the bot's own
Telegram `@handle` instead of 'owner' — Telegram rejects a bot messaging itself
("the bot can't send messages to the bot"), silently dropping what was meant
to be an owner notification. A bot can never legitimately be its own message
recipient, so `perform_message_send` now treats its own (live, per-surface)
bot_username the same as the existing 'owner'/'me' aliases.
"""
import asyncio
import os
import tempfile

from core.surfaces.correspondents import CorrespondentRegistry
from core.surfaces.outbound_allowlist import OutboundAllowlist
from tools.controller.message_send import perform_message_send


class _Router:
    def __init__(self, bot_username=None):
        self.sent = []
        self._bot_username = bot_username

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((surface_id, chat_id, text))
        return True

    def capabilities(self, surface_id):
        return None

    def bot_username(self, surface_id):
        return self._bot_username


def _fixtures():
    tmp = tempfile.mkdtemp()
    corr = CorrespondentRegistry(os.path.join(tmp, "corr.db"))
    allowlist = OutboundAllowlist(os.path.join(tmp, "a.db"))
    return corr, allowlist


def test_own_bot_username_resolves_to_owner():
    _, allowlist = _fixtures()
    router = _Router(bot_username="tmachinroBot")
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="@tmachinroBot", text="done",
        session_id="sess-1"))
    assert res["success"] is True
    assert res["tier"] == "owner"
    assert router.sent == [("telegram", "28436760", "done")]


def test_own_bot_username_matches_case_and_at_insensitively():
    _, allowlist = _fixtures()
    router = _Router(bot_username="tmachinroBot")
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="tmachinrobot", text="done",
        session_id="sess-1"))
    assert res["success"] is True
    assert res["tier"] == "owner"


def test_unrelated_target_is_not_aliased():
    _, allowlist = _fixtures()
    router = _Router(bot_username="tmachinroBot")
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="@someone_else", text="hi",
        session_id="sess-1"))
    assert res["tier"] == "denied"


def test_router_without_bot_username_method_is_fail_soft():
    class _NoUsernameRouter:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, surface_id="telegram", media=None):
            self.sent.append((surface_id, chat_id, text))
            return True

        def capabilities(self, surface_id):
            return None

    _, allowlist = _fixtures()
    router = _NoUsernameRouter()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="@tmachinroBot", text="hi",
        session_id="sess-1"))
    assert res["tier"] == "denied"
