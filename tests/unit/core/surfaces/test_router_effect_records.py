"""033: the MessageRouter seam of the effect recorder.

Both coequal send methods record a DELIVERED send that no Controller action
made; an owner-bound send (the owner lane) and a send inside an action batch
(the Controller hook's) are not recorded here.
"""
import pytest

import core.event_log as el
from core.exec_identity import reset_exec_identity, set_exec_identity
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities
from core.surfaces.message_router import MessageRouter
from core.surfaces.session_chat_registry import SessionChatRegistry
from core.surfaces.surface import Surface


class _Capture(Surface):
    def __init__(self, ok=True):
        super().__init__()
        self.sent = []
        self._ok = ok

    @property
    def surface_id(self):
        return "telegram"

    @property
    def capabilities(self):
        return SurfaceCapabilities(supports_streaming=True)

    async def send(self, msg):
        self.sent.append(msg)
        return SendResult(success=self._ok)

    async def start(self, c):
        pass

    async def stop(self):
        pass


@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "111")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111")
    el._INSTANCES.clear()
    yield lambda: el.get_event_log().query(kind="external_write")
    el._INSTANCES.clear()


def _router(tmp_path, chat_id="555", ok=True):
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    reg.bind("k1", "sess_1", "u1", "telegram", chat_id)
    r = MessageRouter(reg)
    surf = _Capture(ok=ok)
    r.subscribe("telegram", surf)
    return r, surf


@pytest.mark.asyncio
async def test_publish_to_a_correspondent_records_comms(tmp_path, tlog):
    r, surf = _router(tmp_path)
    assert await r.publish(OutboundMessage(session_key="k1", text="hello there"))
    rows = tlog()
    assert len(rows) == 1
    a = rows[0]["attrs"]
    assert rows[0]["effect"] == "comms"
    assert (a["tool"], a["action"], a["surface"]) == ("", "router_publish", "telegram")
    assert "hello" not in str(a)


@pytest.mark.asyncio
async def test_send_message_records_comms(tmp_path, tlog):
    r, _ = _router(tmp_path)
    assert await r.send_message("777", "a proactive note", "telegram")
    rows = tlog()
    assert [x["attrs"]["action"] for x in rows] == ["router_send_message"]


@pytest.mark.asyncio
async def test_owner_bound_send_is_the_owner_lane(tmp_path, tlog, monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "u1")
    r, _ = _router(tmp_path, chat_id="111")
    assert await r.publish(OutboundMessage(session_key="k1", text="hi owner"))
    assert await r.send_message("111", "hi owner again", "telegram")
    assert tlog() == []


@pytest.mark.asyncio
async def test_a_failed_or_dropped_send_records_nothing(tmp_path, tlog):
    r, _ = _router(tmp_path, ok=False)
    assert not await r.publish(OutboundMessage(session_key="k1", text="nope"))
    assert not await r.publish(OutboundMessage(session_key="unbound", text="nope"))
    assert tlog() == []


@pytest.mark.asyncio
async def test_a_send_inside_an_action_batch_is_left_to_the_hook(tmp_path, tlog):
    r, _ = _router(tmp_path)
    tok = set_exec_identity("u1", "sess_1")
    try:
        assert await r.send_message("777", "from inside a message action", "telegram")
    finally:
        reset_exec_identity(tok)
    assert tlog() == []
