"""WS-A: act_on_inbound routes CORRESPONDENT_DATA to the data-delivery seam.

A correspondent reply must be handed to deliver_correspondent_data with the
ORIGINATING session_id and the sender's external address — never _start_task_session
(no third party may spawn/steer a session).
"""
import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import act_on_inbound
from surfaces.telegram.inbound import InboundResult


class _TaskAgent:
    def __init__(self):
        self.calls = []
        self.started = []

    async def deliver_correspondent_data(self, session_id, source, text, metadata=None):
        self.calls.append((session_id, source, text))
        return True


@pytest.mark.asyncio
async def test_correspondent_data_routes_to_delivery_not_session_start():
    src = SessionSource(surface_id="email", chat_id="c1", chat_type="dm", thread_id="t1")
    ident = Identity(user_id="u_john", source=src, raw_user_id="john@acme.com")
    inbound = InboundMessage(text="the invoice is paid", identity=ident)
    decision = RouteDecision(RouteKind.CORRESPONDENT_DATA, "key", session_id="orig_sess")
    ta = _TaskAgent()
    reply = await act_on_inbound(ta, InboundResult(inbound=inbound, decision=decision))
    assert reply is None
    assert ta.calls == [("orig_sess", "john@acme.com", "the invoice is paid")]
