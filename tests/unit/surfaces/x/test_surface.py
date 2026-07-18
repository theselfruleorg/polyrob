"""XSurface — target resolution + chunked DM send (no network)."""
import asyncio

from core.surfaces.envelopes import OutboundMessage
from surfaces.x.surface import XSurface, participant_id_from_session_key, _X_DM_MAX


class FakeClient:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_dm(self, participant_id, text):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((participant_id, text))
        return {"dm_event_id": f"e{len(self.sent)}"}


def test_participant_from_chat_scoped_key():
    assert participant_id_from_session_key("agent:main:x:dm:42:u_x_42") == "42"


def test_participant_from_direct_key():
    assert participant_id_from_session_key("direct:x:42") == "42"


def test_capabilities():
    caps = XSurface(FakeClient()).capabilities
    assert caps.supports_edit is False
    assert caps.max_message_bytes == _X_DM_MAX == 10000


def test_send_short_message():
    client = FakeClient()
    res = asyncio.run(XSurface(client).send(
        OutboundMessage(session_key="agent:main:x:dm:42:u_x_42", text="yo")))
    assert res.success is True
    assert client.sent == [("42", "yo")]


def test_send_splits_over_dm_cap():
    client = FakeClient()
    long = "a" * (_X_DM_MAX + 5)
    res = asyncio.run(XSurface(client).send(
        OutboundMessage(session_key="agent:main:x:dm:42:u_x_42", text=long)))
    assert res.success is True
    assert len(client.sent) == 2
    assert client.sent[0][1] == "a" * _X_DM_MAX
    assert client.sent[1][1] == "a" * 5


def test_send_fail_open():
    res = asyncio.run(XSurface(FakeClient(fail=True)).send(
        OutboundMessage(session_key="agent:main:x:dm:42:u_x_42", text="yo")))
    assert res.success is False
    assert res.error
