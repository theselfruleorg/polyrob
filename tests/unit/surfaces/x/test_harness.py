"""XHarness — dedup + route + deliver wiring with a fake client (no network)."""
import asyncio

from core.surfaces.idempotency import IdempotencyStore
from surfaces.x.harness import XHarness, XSink


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_dm(self, participant_id, text):
        self.sent.append((participant_id, text))
        return {"dm_event_id": "e1"}


def _event(eid="777", sender="42", text="hello"):
    return {"id": eid, "event_type": "MessageCreate", "text": text,
            "sender_id": sender, "dm_conversation_id": f"{sender}-999"}


def _harness(tmp_path, client=None):
    dedup = IdempotencyStore(str(tmp_path / "x_dedup.db"))
    return XHarness(container=None, task_agent=object(),
                    client=client or FakeClient(), dedup=dedup,
                    bot_user_id="999")


def test_dedup_second_delivery_dropped(tmp_path):
    h = _harness(tmp_path)
    routed = []

    async def _fake_route(inbound):
        routed.append(inbound.idempotency_key)

    h._route = _fake_route
    asyncio.run(h.handle_event(_event()))
    asyncio.run(h.handle_event(_event()))
    assert routed == ["777"]


def test_own_message_never_routed(tmp_path):
    h = _harness(tmp_path)
    routed = []

    async def _fake_route(inbound):
        routed.append(inbound)

    h._route = _fake_route
    asyncio.run(h.handle_event(_event(sender="999")))
    assert routed == []


def test_route_delivers_reply_via_send_dm(tmp_path, monkeypatch):
    client = FakeClient()
    h = _harness(tmp_path, client=client)

    class _Decision:
        kind = "task_agent"
        session_key = "agent:main:x:dm:42:u_x_42"
        session_id = None
        silent = False

    async def fake_route_inbound(container, inbound):
        return _Decision()

    async def fake_act_on_inbound(task_agent, result, *, spawn=None, deliver=None):
        assert deliver is not None
        await deliver("streamed part")
        return "final reply"

    import core.surfaces.dispatcher as dispatcher_mod
    import surfaces.telegram.harness as tg_harness_mod
    monkeypatch.setattr(dispatcher_mod, "route_inbound", fake_route_inbound)
    monkeypatch.setattr(tg_harness_mod, "act_on_inbound", fake_act_on_inbound)

    asyncio.run(h.handle_event(_event()))
    assert ("42", "streamed part") in client.sent
    assert ("42", "final reply") in client.sent


def test_sink_send_message(tmp_path):
    client = FakeClient()
    sink = XSink(client)
    ok = asyncio.run(sink.send_message("42", "ping"))
    assert ok is True
    assert client.sent == [("42", "ping")]


def test_sink_fail_open(tmp_path):
    class _Boom:
        async def send_dm(self, *a):
            raise RuntimeError("boom")

    ok = asyncio.run(XSink(_Boom()).send_message("42", "ping"))
    assert ok is False
