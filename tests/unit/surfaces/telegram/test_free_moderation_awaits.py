"""D10: the free moderation path must AWAIT its rights probe.

`_rights_fn` returns an ASYNC probe. `_apply_free` called it without awaiting,
then asked `eff.telegram_right not in held` — and `in` against a coroutine is a
TypeError. So the owner's and a room admin's FREE `/mute`, `/ban`, `/unmute`
and `/unban` raised before ever reaching Telegram: the one path that costs
nothing and is used most.

Same family as the 2026-09-15 "got Future attached to a different loop" on a
free `/ban` — an async seam driven as if it were synchronous.
"""
import asyncio

import pytest

from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.dispatcher import RouteDecision, RouteKind
from surfaces.telegram import group_ops
from surfaces.telegram.inbound import InboundResult


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _result(text, *, chat_role="owner", user_id="rob"):
    source = SessionSource(surface_id="telegram", chat_id="-100",
                           chat_type="supergroup")
    identity = Identity(user_id=user_id, source=source, raw_user_id=user_id,
                        chat_role=chat_role)
    inbound = InboundMessage(
        text=text, identity=identity,
        raw={"message": {"message_id": 5,
                         "chat": {"id": -100, "type": "supergroup"},
                         "from": {"id": user_id},
                         "reply_to_message": {
                             "message_id": 4,
                             "from": {"id": 9911, "first_name": "S"}}}})
    return InboundResult(
        inbound=inbound,
        decision=RouteDecision(RouteKind.COMMAND,
                               "agent:main:telegram:supergroup:-100",
                               command=text.split()[0]))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def wired(monkeypatch):
    """A rights probe that is genuinely ASYNC, as the real one is."""
    granted = {"can_restrict_members"}

    def _rights(task_agent):
        async def _probe(surface, chat_id):
            return set(granted)
        return _probe

    monkeypatch.setattr(group_ops, "_rights_fn", _rights)
    monkeypatch.setattr(group_ops, "_bot", lambda ta: object())

    async def _protection(container, surface, chat_id, target, requester, *,
                          eff, target_status_fn):
        return False, ()

    monkeypatch.setattr("core.surfaces.room_actions._target_protection",
                        _protection)

    performed = []

    async def _perform(task_agent, row, eff, until_ts):
        performed.append((row.verb, row.target_user_id))
        from types import SimpleNamespace
        return SimpleNamespace(ok=True, reason="")

    monkeypatch.setattr(group_ops, "_perform", _perform)
    return performed


@pytest.mark.parametrize("verb,args", [("mute", ["2h"]), ("ban", ["1d"])])
def test_an_owners_free_action_reaches_telegram(env, wired, verb, args):
    handler = {"mute": group_ops.mute_reply, "ban": group_ops.ban_reply}[verb]
    out = asyncio.run(handler(_Agent(str(env)), _result(f"/{verb} {args[0]}"),
                              args))
    text = getattr(out, "text", out)
    assert "❌" not in text, text
    assert wired == [(verb, "9911")]


def test_a_missing_right_is_named_not_a_traceback(env, monkeypatch, wired):
    """The refusal this probe exists for must still fire — and say which
    permission is missing."""
    def _none(task_agent):
        async def _probe(surface, chat_id):
            return set()
        return _probe

    monkeypatch.setattr(group_ops, "_rights_fn", _none)
    out = asyncio.run(group_ops.mute_reply(_Agent(str(env)),
                                           _result("/mute 2h"), ["2h"]))
    text = getattr(out, "text", out)
    assert "permission" in text and "❌" in text
    assert wired == []


def test_a_sync_probe_double_still_works(env, monkeypatch, wired):
    """`_awaited` tolerates a plain-function test double, so the doubles in
    this suite stay readable without pretending production is synchronous."""
    def _sync(task_agent):
        def _probe(surface, chat_id):
            return {"can_restrict_members"}
        return _probe

    monkeypatch.setattr(group_ops, "_rights_fn", _sync)
    out = asyncio.run(group_ops.mute_reply(_Agent(str(env)),
                                           _result("/mute 2h"), ["2h"]))
    assert "❌" not in getattr(out, "text", out)
