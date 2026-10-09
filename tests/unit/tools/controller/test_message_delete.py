"""0008: send -> the receipt carries the Telegram message_id and a post-ledger row
-> the owner tells the agent to delete it -> Bot API deleteMessage.

Real MessageRouter + real TelegramSurface over a fake bot (no network); the post
ledger is the ownership proof, Telegram's 48 h window is checked before the call,
and an autonomous/forged turn can never delete.
"""
import asyncio
import time

import pytest

from core.surfaces.message_router import MessageRouter
from core.surfaces.outbound_allowlist import OutboundAllowlist
from core.surfaces.sent_posts import SentPosts
from core.surfaces.session_chat_registry import SessionChatRegistry
from surfaces.telegram.surface import TelegramSurface
from tools.controller import message_send as ms

DEN = "-1002125904710"


class _Msg:
    def __init__(self, mid): self.message_id = mid


class _Bot:
    def __init__(self):
        self._next = 900
        self.sent, self.deleted = [], []

    async def send_message(self, chat_id, text, **kw):
        self._next += 1
        self.sent.append((chat_id, self._next))
        return _Msg(self._next)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((str(chat_id), int(message_id)))
        return True


class _Ctx:
    """An owner turn on an unbound install: the owner principal IS ``local``."""
    def __init__(self, role="orchestrator", user_id="local", metadata=None,
                 is_sub_agent=False):
        self.role, self.is_sub_agent = role, is_sub_agent
        self.session_id, self.user_id = "s-owner", user_id
        self.metadata = metadata or {}


class _Controller:
    _is_sub_agent = False
    session_id = "s-owner"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "false")
    for k in ("POLYROB_OWNER_TELEGRAM_ID", "OWNER_USER_ID", "ALLOWED_TELEGRAM_USER_IDS"):
        monkeypatch.delenv(k, raising=False)
    db = str(tmp_path / "surfaces.db")
    router = MessageRouter(SessionChatRegistry(db))
    bot = _Bot()
    router.subscribe("telegram", TelegramSurface(bot))
    posts = SentPosts(db)
    router.attach_sent_posts(posts)
    allow = OutboundAllowlist(db)
    allow.allow("rob", "telegram", DEN)
    return router, bot, posts, allow


def _send(router, allow, text="6551 tease", target=DEN):
    return asyncio.run(ms.perform_message_send(
        router=router, allowlist=allow, owner_targets={"telegram": "111"},
        user_id="rob", surface="telegram", target=target, text=text))


def _delete(router, ctx=None, **kw):
    kw.setdefault("surface", "telegram")
    return asyncio.run(ms.perform_message_delete(
        router=router, owner_targets={"telegram": "111"},
        execution_context=ctx, controller=_Controller(), **kw))


def test_receipt_carries_message_id_and_post_row(rig):
    router, bot, posts, allow = rig
    res = _send(router, allow)
    assert res["success"] is True
    assert res["message_id"] == str(bot.sent[-1][1])
    assert res["message_ids"] == [str(bot.sent[-1][1])]
    row = posts.get(res["post"])
    assert row.chat_id == DEN and row.message_ids == [res["message_id"]]


def test_send_record_delete_by_post_row(rig):
    router, bot, posts, allow = rig
    res = _send(router, allow)
    out = _delete(router, _Ctx(), post=res["post"])
    assert out["success"] is True, out
    assert bot.deleted == [(DEN, int(res["message_id"]))]
    assert posts.get(res["post"]).deleted_ts is not None
    # a second delete of the same row is named, not re-sent to Telegram
    again = _delete(router, _Ctx(), post=res["post"])
    assert again["success"] is False and "already deleted" in again["error"]
    assert len(bot.deleted) == 1


def test_delete_by_message_id_in_target_chat(rig):
    router, bot, posts, allow = rig
    res = _send(router, allow)
    out = _delete(router, _Ctx(), target=DEN, message_id=res["message_id"])
    assert out["success"] is True, out
    assert bot.deleted == [(DEN, int(res["message_id"]))]


def test_delete_last_n_posts_to_chat(rig):
    router, bot, posts, allow = rig
    first = _send(router, allow, "one")
    second = _send(router, allow, "two")
    out = _delete(router, _Ctx(), target=DEN, last=2)
    assert out["success"] is True, out
    assert sorted(m for _, m in bot.deleted) == sorted(
        [int(first["message_id"]), int(second["message_id"])])
    assert sorted(out["deleted_posts"]) == sorted([first["post"], second["post"]])


def test_refuses_message_that_is_not_our_own(rig):
    router, bot, posts, allow = rig
    _send(router, allow)
    out = _delete(router, _Ctx(), target=DEN, message_id="12345")
    assert out["success"] is False
    assert "not one of my own posts" in out["error"]
    assert bot.deleted == []


def test_refuses_post_older_than_48h(rig, monkeypatch):
    router, bot, posts, allow = rig
    row = posts.record("telegram", DEN, ["77"], "old tease", now=time.time() - 49 * 3600)
    out = _delete(router, _Ctx(), post=row)
    assert out["success"] is False
    assert "48 h" in out["error"]
    assert bot.deleted == []


def test_refuses_autonomous_turn(rig):
    router, bot, posts, allow = rig
    res = _send(router, allow)
    out = _delete(router, _Ctx(role="leaf"), post=res["post"])
    assert out["success"] is False
    assert "owner" in out["error"]
    assert bot.deleted == []


def test_unknown_post_row_is_named(rig):
    router, bot, posts, allow = rig
    out = _delete(router, _Ctx(), post=9999)
    assert out["success"] is False and "no post #9999" in out["error"]


def test_telegram_refusal_is_returned_and_row_kept(rig):
    router, bot, posts, allow = rig
    res = _send(router, allow)

    async def _boom(chat_id, message_id):
        raise RuntimeError("Bad Request: message can't be deleted for everyone")
    bot.delete_message = _boom
    out = _delete(router, _Ctx(), post=res["post"])
    assert out["success"] is False
    assert "can't be deleted" in out["error"]
    assert posts.get(res["post"]).deleted_ts is None


def test_posts_lists_what_was_posted(rig):
    router, bot, posts, allow = rig
    res = _send(router, allow, "the 6551 tease")
    out = asyncio.run(ms.perform_message_posts(
        router=router, owner_targets={"telegram": "111"}, surface="telegram",
        target=DEN, last=5, execution_context=_Ctx(), controller=_Controller()))
    assert out["success"] is True
    assert out["posts"][0]["post"] == res["post"]
    assert out["posts"][0]["deletable"] is True
    assert "6551 tease" in out["posts"][0]["preview"]


def test_router_without_ledger_still_sends(tmp_path, monkeypatch):
    """A router that predates the ledger (no attach) keeps the old receipt."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    db = str(tmp_path / "surfaces.db")
    router = MessageRouter(SessionChatRegistry(db))
    router.subscribe("telegram", TelegramSurface(_Bot()))
    allow = OutboundAllowlist(db)
    allow.allow("rob", "telegram", DEN)
    res = _send(router, allow)
    assert res["success"] is True and res["message_id"] and res.get("post") is None


def test_rendered_receipt_names_message_id_and_post(rig):
    from tools.controller.turn_origin import _message_action_result
    router, bot, posts, allow = rig
    res = _send(router, allow)
    out = _message_action_result(res, "telegram", DEN, "6551 tease")
    assert f"message_id {res['message_id']}" in out.extracted_content
    assert f"post={res['post']}" in out.extracted_content


def test_rendered_delete_and_posts(rig):
    from tools.controller.turn_origin import _message_action_result
    router, bot, posts, allow = rig
    res = _send(router, allow)
    listed = asyncio.run(ms.perform_message_posts(
        router=router, owner_targets={}, surface="telegram", target=DEN,
        execution_context=_Ctx(), controller=_Controller()))
    out = _message_action_result(listed, "telegram", DEN, "")
    assert f"post #{res['post']}" in out.extracted_content
    gone = _delete(router, _Ctx(), post=res["post"])
    out = _message_action_result(gone, "telegram", DEN, "")
    assert out.error is None and f"#{res['post']}" in out.extracted_content


def test_action_entry_routes_delete_and_refuses_empty_send(rig):
    router, bot, posts, allow = rig
    res = _send(router, allow)
    out = asyncio.run(ms.perform_message_action(
        action="delete", post=res["post"], router=router, allowlist=allow,
        owner_targets={}, user_id="rob", surface="telegram", target=None,
        execution_context=_Ctx(), controller=_Controller()))
    assert out["success"] is True
    empty = asyncio.run(ms.perform_message_action(
        action="send", text="  ", router=router, allowlist=allow, owner_targets={},
        user_id="rob", surface="telegram", target=DEN))
    assert empty["success"] is False and "empty" in empty["error"]


@pytest.mark.parametrize("ctx", [
    _Ctx(metadata={"turn_kind": "group"}),       # a room turn
    _Ctx(is_sub_agent=True),                      # a delegated sub-agent
    _Ctx(user_id="u_stranger"),                   # another tenant
    None,                                         # no context at all
], ids=["room", "sub_agent", "other_tenant", "no_context"])
def test_non_owner_turns_cannot_delete_or_list(rig, ctx):
    router, bot, posts, allow = rig
    res = _send(router, allow)
    out = _delete(router, ctx, post=res["post"])
    assert out["success"] is False and bot.deleted == []
    listed = asyncio.run(ms.perform_message_posts(
        router=router, owner_targets={}, surface="telegram", target=None,
        execution_context=ctx, controller=_Controller()))
    assert listed["success"] is False and "posts" not in listed


def test_post_row_in_another_chat_than_target_is_refused(rig):
    router, bot, posts, allow = rig
    res = _send(router, allow)
    out = _delete(router, _Ctx(), post=res["post"], target="-100999")
    assert out["success"] is False and "not telegram:-100999" in out["error"]
    assert bot.deleted == []


@pytest.mark.parametrize("n", [0, -3, 11, "x"])
def test_last_out_of_range_is_refused(rig, n):
    router, bot, posts, allow = rig
    _send(router, allow)
    out = _delete(router, _Ctx(), target=DEN, last=n)
    assert out["success"] is False and "1..10" in out["error"]
    assert bot.deleted == []


def test_message_id_deletes_the_whole_post(rig):
    router, bot, posts, allow = rig
    row = posts.record("telegram", DEN, ["501", "502"], "two chunks")
    out = _delete(router, _Ctx(), target=DEN, message_id="502")
    assert out["success"] is True
    assert sorted(m for _, m in bot.deleted) == [501, 502]
    assert posts.get(row).deleted_ts is not None


def test_already_removed_by_hand_counts_as_deleted(rig):
    router, bot, posts, allow = rig
    res = _send(router, allow)

    async def _gone(chat_id, message_id):
        raise RuntimeError("Bad Request: message to delete not found")
    bot.delete_message = _gone
    out = _delete(router, _Ctx(), post=res["post"])
    assert out["success"] is True
    assert posts.get(res["post"]).deleted_ts is not None


def test_correspondent_reply_exemption_never_covers_delete():
    from agents.task.agent.core.correspondent_gate import _reply_target
    assert _reply_target("message", {"surface": "telegram", "target": "42",
                                     "action": "delete", "post": 1}) == ("", "")
    assert _reply_target("message", {"surface": "telegram", "target": "42",
                                     "action": "posts"}) == ("", "")
    assert _reply_target("message", {"surface": "telegram", "target": "42",
                                     "text": "hi"}) == ("telegram", "42")
