"""Telegram's synthetic from user is not an authenticated room principal."""
from types import SimpleNamespace

import pytest

from surfaces.telegram.harness import _tg_user_id, _is_owner_groups_line
from surfaces.telegram.inbound import build_inbound_message


@pytest.mark.parametrize("fake_uid", [1087968824, 136817688, 777000, 12345])
def test_sender_chat_never_becomes_a_person(fake_uid, monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_USER_ID", str(fake_uid))
    update = {"message": {"chat": {"id": -100, "type": "supergroup"},
                          "from": {"id": fake_uid, "is_bot": True},
                          "sender_chat": {"id": -100, "title": "Anon"},
                          "text": "/groups allow here"}}
    assert _tg_user_id(update) is None
    assert not _is_owner_groups_line(update)
    assert build_inbound_message(update, None) is None


def test_channel_identity_is_the_channel_not_shared_fake_user():
    update = {"channel_post": {"chat": {"id": -100, "type": "channel"},
                               "from": {"id": 136817688, "is_bot": True},
                               "sender_chat": {"id": -100}, "text": "hello"}}
    directory = SimpleNamespace(resolve_internal=lambda uid, _: f"channel:{uid}")
    inbound = build_inbound_message(update, directory)
    assert inbound.identity.raw_user_id == "-100"
    assert inbound.identity.user_id == "channel:-100"
