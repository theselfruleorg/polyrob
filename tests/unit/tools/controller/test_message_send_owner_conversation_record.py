"""2026-08-27 dedup-guard fix, part 2: a send to the OWNER must still leave a
durable record in the conversation store — the owner-resend cooldown guard
(`_autonomous_owner_resend_cooldown_refusal`, turn_origin.py) reads this SAME
store, and without a record here it can never see a real owner send.

Confirmed live: the cooldown guard shipped, but a 5th duplicate owner-ask
send still went out 11 minutes after deploy, because the ORIGINAL
`record_outbound` call is deliberately skipped for tier=="owner" (comment:
"owner targets are not correspondent conversations") — that skip is correct
for the seed/first-contact/daily-cap machinery, but left the cooldown guard
reading an empty/stale store.
"""
import asyncio
import os
import tempfile

from core.surfaces.conversations import ConversationStore
from core.surfaces.outbound_allowlist import OutboundAllowlist
from tools.controller.message_send import perform_message_send


class _Router:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((surface_id, chat_id, text))
        return True

    def capabilities(self, surface_id):
        return None


class _Container:
    def __init__(self, store):
        self._store = store

    def get_service(self, name):
        return self._store if name == "conversation_store" else None


def _store():
    tmp = tempfile.mkdtemp()
    return ConversationStore(os.path.join(tmp, "conv.db"))


def test_owner_send_is_recorded_in_conversation_store():
    store = _store()
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=None, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="owner", text="the ask",
        session_id="sess-1", container=_Container(store)))
    assert res["success"] is True
    assert res["tier"] == "owner"

    # The cooldown guard's exact read path: keyed by the RESOLVED address,
    # never the literal 'owner' alias.
    assert store.outbound_count_since("rob", "telegram", "28436760", 3600) == 1
    conv = store.get("rob", "telegram", "28436760")
    assert conv is not None


def test_owner_send_by_resolved_address_directly_also_recorded():
    """The model may pass the resolved address instead of the 'owner' alias."""
    store = _store()
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=None, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="28436760", text="the ask",
        session_id="sess-1", container=_Container(store)))
    assert res["success"] is True
    assert store.outbound_count_since("rob", "telegram", "28436760", 3600) == 1


def test_repeat_owner_sends_accumulate_in_the_store():
    store = _store()
    router = _Router()
    for i in range(3):
        asyncio.run(perform_message_send(
            router=router, allowlist=None, owner_targets={"telegram": "28436760"},
            user_id="rob", surface="telegram", target="owner", text=f"ask {i}",
            session_id=f"sess-{i}", container=_Container(store)))
    assert store.outbound_count_since("rob", "telegram", "28436760", 3600) == 3


def test_non_owner_send_unaffected_by_this_change():
    """Regression: the pre-existing non-owner record path is untouched."""
    store = _store()
    tmp = tempfile.mkdtemp()
    allowlist = OutboundAllowlist(os.path.join(tmp, "a.db"))
    allowlist.allow("rob", "telegram", "@some_promo_chat")
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="@some_promo_chat", text="hi",
        session_id="sess-1", container=_Container(store)))
    assert res["success"] is True
    assert res["tier"] != "owner"
    assert store.outbound_count_since("rob", "telegram", "@some_promo_chat", 3600) == 1


def test_send_failure_records_nothing():
    class _FailingRouter(_Router):
        async def send_message(self, chat_id, text, surface_id="telegram", media=None):
            return False

    store = _store()
    res = asyncio.run(perform_message_send(
        router=_FailingRouter(), allowlist=None, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="owner", text="the ask",
        session_id="sess-1", container=_Container(store)))
    assert res["success"] is False
    assert store.outbound_count_since("rob", "telegram", "28436760", 3600) == 0
