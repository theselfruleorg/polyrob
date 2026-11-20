"""A carded reply to a Slack thread goes back INTO the thread: the session key
_send_with_actions publishes carries `:thread:<ts>`, and the Slack surface reads
the thread from that key (verified 2026-11-20; a regression guard)."""
import asyncio
from types import SimpleNamespace

from core.surfaces.command_reply import CommandReply
from core.surfaces.envelopes import Action, Identity, InboundMessage, SessionSource


class _Router:
    def __init__(self):
        self.sent = []

    async def publish(self, msg):
        self.sent.append(msg)
        return True


def _container(router):
    surface = SimpleNamespace(capabilities=SimpleNamespace(supports_actions=True))
    services = {"surface_registry": SimpleNamespace(get=lambda sid: surface),
                "message_router": router}
    return SimpleNamespace(get_service=services.get)


def _inbound(chat_type, thread):
    src = SessionSource(surface_id="slack", chat_id="C1", chat_type=chat_type,
                        thread_id=thread)
    return InboundMessage(text="/status", identity=Identity(user_id="u1", source=src))


def test_a_threaded_carded_reply_keeps_its_thread():
    from surfaces._shared import _send_with_actions
    from surfaces.slack.surface import thread_ts_from_session_key
    for chat_type in ("dm", "group"):
        router = _Router()
        reply = CommandReply(text="ok", actions=(Action(label="Status", command="/status"),))
        assert asyncio.run(_send_with_actions(_container(router),
                                              _inbound(chat_type, "1700000000.000100"),
                                              reply, "/status"))
        key = router.sent[0].session_key
        assert thread_ts_from_session_key(key) == "1700000000.000100", key


def test_an_unthreaded_reply_has_no_thread():
    from surfaces._shared import _send_with_actions
    from surfaces.slack.surface import thread_ts_from_session_key
    router = _Router()
    reply = CommandReply(text="ok", actions=(Action(label="Status", command="/status"),))
    asyncio.run(_send_with_actions(_container(router), _inbound("dm", None), reply, "/status"))
    assert thread_ts_from_session_key(router.sent[0].session_key) is None
