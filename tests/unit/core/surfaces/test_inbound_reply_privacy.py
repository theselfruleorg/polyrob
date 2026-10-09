from types import SimpleNamespace

import pytest

from core.surfaces.command_reply import CommandReply, admit_inbound_reply
from core.surfaces.dispatcher import RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource


def inbound(chat_type='group', sender='caller'):
    return InboundMessage(text='/status', identity=Identity(user_id=sender, raw_user_id=sender,
        source=SessionSource(surface_id='slack', chat_id='room', chat_type=chat_type)))


@pytest.mark.parametrize('kind', [RouteKind.COMMAND, RouteKind.STEER])
def test_room_owner_commands_require_explicit_room_destination(kind):
    decision = SimpleNamespace(kind=kind)
    assert not admit_inbound_reply(inbound(), decision, 'private settings')
    assert not admit_inbound_reply(inbound(), decision, CommandReply('private settings'))
    assert admit_inbound_reply(inbound(), decision, CommandReply('public price quote', to_room=True))
    assert admit_inbound_reply(inbound('dm'), decision, 'private settings')


def test_group_conversation_reply_is_public():
    assert admit_inbound_reply(inbound(), SimpleNamespace(kind=RouteKind.GROUP_TURN), 'answer')


def test_denials_stay_private_and_are_bounded_per_sender_and_globally():
    decision = SimpleNamespace(kind=RouteKind.DENIED, silent=False)
    assert not admit_inbound_reply(inbound(), decision, 'pairing code')
    assert admit_inbound_reply(inbound('dm'), decision, 'pairing code')
    assert not admit_inbound_reply(inbound('dm'), decision, 'pairing code')
    allowed = sum(admit_inbound_reply(inbound('dm', f'user-{i}'), decision, 'denied') for i in range(100))
    assert allowed == 49


def test_silent_denials_never_send():
    assert not admit_inbound_reply(inbound('dm'), SimpleNamespace(kind=RouteKind.DENIED, silent=True), 'denied')
