"""2026-08-27 dedup-guard fix, part 2 — re-based on 061 (2026-09-22): a send to
the OWNER must still leave a durable record the owner-resend cooldown guard can
read. The record now lives in the ONE owner thread (`core/surfaces/owner_thread`),
never in the correspondent conversation store: an autonomous session's `message`
send is recorded by the tool's owner branch; an interactive session's by the turn
latch (`mark_reply_published`). Either way the cooldown reads the thread."""
import asyncio
import os
import tempfile

from core.surfaces import owner_thread as ot
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
    def __init__(self, store, data_dir):
        self._store = store
        self.config = type("Cfg", (), {"data_dir": data_dir})()
        self._svc = {}

    def get_service(self, name):
        if name == "conversation_store":
            return self._store
        return self._svc.get(name)

    def register_service(self, name, svc):
        self._svc[name] = svc


def _env():
    tmp = tempfile.mkdtemp()
    store = ConversationStore(os.path.join(tmp, "conv.db"))
    return store, _Container(store, tmp)


def _autonomous(sid):
    from agents.task.goals.autonomy_marker import mark_autonomous
    mark_autonomous(sid, "goal-x")


def _send(container, target, text, sid, controller=None):
    return asyncio.run(perform_message_send(
        router=_Router(), allowlist=None, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target=target, text=text,
        session_id=sid, container=container, controller=controller))


def test_autonomous_owner_send_is_recorded_in_the_owner_thread():
    store, c = _env()
    _autonomous("sess-auto-1")
    res = _send(c, "owner", "the ask", "sess-auto-1")
    assert res["success"] is True and res["tier"] == "owner"
    rows = ot.thread_recent(c, "rob", limit=5)
    assert [(r["via"], r["source"], r["session_id"]) for r in rows] == [("telegram", "message_tool", "sess-auto-1")]
    # never the correspondent store — the owner is not a correspondent
    assert store.get("rob", "telegram", "28436760") is None
    # the cooldown guard's exact read path
    assert ot.recent_outbound_bodies(c, "rob", 3600) == ["the ask"]


def test_owner_send_by_prefixed_address_is_owner_tier_and_recorded():
    """§2.3: `telegram:28436760` used to be tier OPEN (a correspondent seed and a
    second conversation row). It is the owner under every spelling now."""
    store, c = _env()
    _autonomous("sess-auto-2")
    res = _send(c, "telegram:28436760", "the ask", "sess-auto-2")
    assert res["success"] is True and res["tier"] == "owner"
    assert len(ot.thread_recent(c, "rob", limit=5)) == 1
    assert store.list("rob") == []


def test_interactive_owner_send_is_recorded_by_the_turn_latch():
    store, c = _env()
    orch = type("O", (), {"container": c, "user_id": "rob", "session_id": "sess-int",
                          "_chat_session_key": "agent:main:telegram:dm:1:rob",
                          "_terminal_attached": False, "_public_session": False})()
    ctl = type("Ctl", (), {"orchestrator": orch})()
    res = _send(c, "owner", "a chat answer with a file", "sess-int", controller=ctl)
    assert res["success"] is True
    rows = ot.thread_recent(c, "rob", limit=5)
    assert [(r["via"], r["source"]) for r in rows] == [("telegram", "reply")]


def test_repeat_owner_sends_accumulate_in_the_thread():
    store, c = _env()
    _autonomous("sess-auto-3")
    for i in range(3):
        _send(c, "owner", f"ask {i}", "sess-auto-3")
    assert len(ot.recent_outbound_bodies(c, "rob", 3600)) == 3


def test_legacy_owner_rows_are_adopted_on_the_first_owner_send():
    store, c = _env()
    store.record_outbound("rob", "telegram", "28436760", "old digest", session_id="s0", now=1000.0)
    store.record_outbound("rob", "telegram", "telegram:28436760", "old typed", session_id="s0", now=2000.0)
    ot._ADOPTED.discard("rob")
    _autonomous("sess-auto-4")
    _send(c, "owner", "new line", "sess-auto-4")
    assert store.list("rob") == []
    assert [r["body"] for r in ot.thread_recent(c, "rob", limit=5)] == ["old digest", "old typed", "new line"]


def test_non_owner_send_unaffected_by_this_change():
    """Regression: the pre-existing non-owner record path is untouched."""
    store, c = _env()
    tmp = tempfile.mkdtemp()
    allowlist = OutboundAllowlist(os.path.join(tmp, "a.db"))
    allowlist.allow("rob", "telegram", "@some_promo_chat")
    res = asyncio.run(perform_message_send(
        router=_Router(), allowlist=allowlist, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="@some_promo_chat", text="hi",
        session_id="sess-1", container=c))
    assert res["success"] is True
    assert res["tier"] != "owner"
    assert store.outbound_count_since("rob", "telegram", "@some_promo_chat", 3600) == 1
    assert ot.thread_recent(c, "rob", limit=5) is None


def test_send_failure_records_nothing():
    class _FailingRouter(_Router):
        async def send_message(self, chat_id, text, surface_id="telegram", media=None):
            return False

    store, c = _env()
    _autonomous("sess-auto-5")
    res = asyncio.run(perform_message_send(
        router=_FailingRouter(), allowlist=None, owner_targets={"telegram": "28436760"},
        user_id="rob", surface="telegram", target="owner", text="the ask",
        session_id="sess-auto-5", container=c))
    assert res["success"] is False
    assert ot.thread_recent(c, "rob", limit=5) is None
