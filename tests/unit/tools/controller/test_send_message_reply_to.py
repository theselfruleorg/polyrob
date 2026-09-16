"""044 T20: `send_message(reply_to=<message id>)` — the verb a room SERVICE run
answers with, and the record of WHICH lines it answered.

A live room turn gets its anchor from ``orchestrator._turn_reply_to`` (T10): the
line that triggered the turn. A service run has no trigger — it reads a tail and
CHOOSES which lines to answer — so the anchor has to be a parameter. The explicit
parameter wins when set; the turn anchor is the fallback, so a live room turn is
byte-identical to before.

The ids a run replied to are recorded on the orchestrator by the mirror itself
(``_room_replied_ids``), because that is the one place that knows both the
session key (is this a ROOM?) and the anchor.
"""
import asyncio
from types import SimpleNamespace

from core.surfaces.outbound_mirror import build_discrete_publish
from tools.controller.emit import publish_context
from tools.controller.views import SendMessageAction


class _Router:
    """Stands in for MessageRouter: `publish` REPORTS whether it delivered."""

    def __init__(self, delivered=True):
        self.published = []
        self._delivered = delivered

    async def publish(self, msg):
        self.published.append(msg)
        return self._delivered


def _orch(key):
    return SimpleNamespace(_chat_session_key=key, _turn_reply_to=None, user_id="rob")


def test_the_action_model_accepts_reply_to():
    params = SendMessageAction(text="yes, live since Tuesday", reply_to="7")
    assert params.reply_to == "7"
    assert SendMessageAction(text="hi").reply_to is None


def test_publish_context_prefers_the_explicit_reply_to():
    orch = _orch("agent:main:telegram:supergroup:-1001")
    orch._turn_reply_to = "1"
    controller = SimpleNamespace(orchestrator=orch, session_id=None, user_id="rob")
    assert publish_context(controller, reply_to="7").get("reply_to") == "7"
    # No explicit anchor => the live turn's anchor, unchanged.
    assert publish_context(controller).get("reply_to") == "1"


def test_a_room_publish_records_the_id_it_answered(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    router = _Router()
    replied = []
    pub = build_discrete_publish(router, "agent:main:telegram:supergroup:-1001",
                                 reply_to="7", replied_ids=replied)
    asyncio.run(pub("yes, live since Tuesday"))
    assert router.published[0].reply_to == "7"
    assert replied == ["7"]


def test_a_dm_publish_records_nothing(monkeypatch):
    """`_room_replied_ids` feeds the room ledger's `answered_by`. A DM has no
    ledger, so a DM reply must never land in it."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    replied = []
    pub = build_discrete_publish(_Router(), "agent:main:telegram:dm:99:rob",
                                 reply_to="7", replied_ids=replied)
    asyncio.run(pub("hi"))
    assert replied == []


def test_no_anchor_records_nothing(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    replied = []
    pub = build_discrete_publish(_Router(), "agent:main:telegram:supergroup:-1001",
                                 replied_ids=replied)
    asyncio.run(pub("just talking"))
    assert replied == []


def test_a_suppressed_publish_is_never_recorded_as_an_answer(monkeypatch):
    """Fix round 1 (Minor 7): a `[SILENT]`, a capped room, a dead target or a
    failed send all return False from `publish` — the room never heard the reply,
    so its ledger line must stay unanswered."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    replied = []
    pub = build_discrete_publish(_Router(delivered=False),
                                 "agent:main:telegram:supergroup:-1001",
                                 reply_to="7", replied_ids=replied)
    asyncio.run(pub("yes"))
    assert replied == []


def test_a_legacy_router_double_records_nothing(monkeypatch):
    """A router that returns None is making no delivery claim. Under-recording
    only costs a re-shown line; over-recording loses a question."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    replied = []
    pub = build_discrete_publish(_Router(delivered=None),
                                 "agent:main:telegram:supergroup:-1001",
                                 reply_to="7", replied_ids=replied)
    asyncio.run(pub("yes"))
    assert replied == []


def test_the_real_router_reports_a_silent_room_reply_as_not_delivered(tmp_path, monkeypatch):
    """The other half of the contract, at the router itself."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    from core.surfaces.envelopes import MessageKind, OutboundMessage, SendResult
    from core.surfaces.message_router import MessageRouter

    class _Registry:
        db_path = str(tmp_path / "surfaces.db")

        def resolve(self, key):
            return {"surface_id": "telegram", "chat_id": "-1001"}

    class _Surface:
        async def send(self, msg):
            return SendResult(success=True)

    router = MessageRouter(_Registry())
    router.subscribe("telegram", _Surface())
    key = "agent:main:telegram:supergroup:-1001"

    def _pub(text):
        return asyncio.run(router.publish(OutboundMessage(
            session_key=key, text=text, kind=MessageKind.AGENT_TEXT, partial=False)))

    assert _pub("[SILENT]") is False
    assert _pub("a real answer") is True
