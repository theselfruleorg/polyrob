import asyncio
from surfaces.telegram.surface import TelegramSurface, thread_id_from_session_key
from core.surfaces.envelopes import OutboundMessage


class _Bot:
    def __init__(self):
        self.calls = []
    async def send_message(self, chat_id, text, **kw):
        self.calls.append((chat_id, text, kw))
        class _M: message_id = 11
        return _M()


def test_thread_id_from_key():
    assert thread_id_from_session_key("agent:main:telegram:supergroup:-100:thread:7") == "7"
    assert thread_id_from_session_key("agent:main:telegram:group:-100") is None


def test_send_sets_reply_and_thread():
    bot = _Bot()
    s = TelegramSurface(bot)
    msg = OutboundMessage(session_key="agent:main:telegram:supergroup:-100:thread:7",
                          text="hi", reply_to="55")
    asyncio.run(s.send(msg))
    _, _, kw = bot.calls[0]
    assert kw.get("reply_to_message_id") == 55
    assert kw.get("message_thread_id") == 7


def test_stale_anchor_self_heals():
    class _Bot2(_Bot):
        async def send_message(self, chat_id, text, **kw):
            if kw.get("reply_to_message_id"):
                raise RuntimeError("Bad Request: message to be replied not found")
            return await super().send_message(chat_id, text, **kw)
    bot = _Bot2()
    s = TelegramSurface(bot)
    asyncio.run(s.send(OutboundMessage(session_key="agent:main:telegram:group:-100",
                                       text="hi", reply_to="55")))
    assert bot.calls and "reply_to_message_id" not in bot.calls[-1][2]


def test_stale_anchor_self_heal_keeps_thread_id():
    """Fix round 1, finding #4: the self-heal retry must drop ONLY the stale
    reply_to_message_id — a regression that also dropped message_thread_id
    (e.g. sharing one `extra` reset instead of popping the single stale key)
    would silently un-thread a forum-topic reply too, and this is the only
    test that would catch it (test_stale_anchor_self_heals above uses a key
    with no thread segment)."""
    class _Bot2(_Bot):
        async def send_message(self, chat_id, text, **kw):
            if kw.get("reply_to_message_id"):
                raise RuntimeError("Bad Request: message to be replied not found")
            return await super().send_message(chat_id, text, **kw)
    bot = _Bot2()
    s = TelegramSurface(bot)
    asyncio.run(s.send(OutboundMessage(
        session_key="agent:main:telegram:supergroup:-100:thread:7",
        text="hi", reply_to="55")))
    _, _, kw = bot.calls[-1]
    assert "reply_to_message_id" not in kw
    assert kw.get("message_thread_id") == 7
