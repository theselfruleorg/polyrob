"""A refusal on an owner turn is named on every thin-client seat.

Slack, Discord, Signal, Feishu, DingTalk (``surfaces._shared.route_and_act``) and
WhatsApp (``core.surfaces.inbound_webhook``) all reach the ONE shared executor
(``surfaces._actor.act_on_inbound`` -> ``_run_and_deliver``), which asks
``run_outcome.session_turn_end_notice`` once per turn. This pins that the owner's
DM gets the refusal line with its category, and no "Say continue".
"""
import types

import pytest

from core.exceptions import REFUSAL_HALT_PREFIX
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from tests.unit.surfaces.telegram.test_harness import _FakeTaskAgent

_REFUSAL = (f"{REFUSAL_HALT_PREFIX}the model declined this request (category: cyber). "
            "Nothing was retried on another model.")


def _refused_orch():
    # The refusal step has no model output; its error is the owner text.
    step = types.SimpleNamespace(model_output=None,
                                 result=[types.SimpleNamespace(error=_REFUSAL)])
    agent = types.SimpleNamespace(
        _is_sub_agent=False, history=types.SimpleNamespace(history=[step]),
        state=types.SimpleNamespace(stop_reason="error", max_steps=50, n_steps=1,
                                    exhausted_summary=None))
    return types.SimpleNamespace(agents={"a1": agent})


class _Agent(_FakeTaskAgent):
    def _chat_turn_baseline(self, session_id):
        return (0, [], "")

    async def run_session(self, user_id, session_id):
        return f"Session failed: {_REFUSAL}"


@pytest.fixture(autouse=True)
def _quiet_thread(monkeypatch):
    monkeypatch.setattr("core.surfaces.owner_thread.record_owner_out",
                        lambda *a, **k: True)
    monkeypatch.setattr("core.surfaces.user_delivery.record_interactive_reply",
                        lambda *a, **k: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["slack", "discord", "signal", "whatsapp",
                                     "feishu", "dingtalk"])
async def test_owner_dm_gets_the_refusal_line(monkeypatch, surface):
    from surfaces._shared import route_and_act
    key = f"agent:main:{surface}:dm:c1"
    decision = RouteDecision(RouteKind.STEER, key, session_id="s1")

    async def _route(container, inbound):
        return decision
    monkeypatch.setattr("core.surfaces.dispatcher.route_inbound", _route)
    inbound = InboundMessage(text="scan this host", identity=Identity(
        user_id="owner", source=SessionSource(surface, "c1", "dm"), raw_user_id="c1"))
    sent, spawned = [], []

    async def deliver(text):
        sent.append(text)
    await route_and_act(None, _Agent(orch=_refused_orch()), inbound, deliver,
                        spawn=spawned.append)
    assert len(spawned) == 1
    await spawned[0]
    assert len(sent) == 1, sent
    assert sent[0].startswith(
        "⚠️ My turn ended early: the model declined this request (category: cyber)")
    assert "Say continue" not in sent[0]
