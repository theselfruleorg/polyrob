"""109 W4: the X thread channel — field mapping, edits, the share, 402, 403, the record."""
import pytest

pytest.importorskip("polyrob_x")

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.commerce.thread import ThreadUnavailable
from polyrob_x import commerce_thread as ct
from polyrob_x import write_budget


def _tweet(i, author="42", conv="100", text="/buy x=1", edits=None, reply_to="100"):
    return SimpleNamespace(
        id=i, author_id=author, conversation_id=conv, text=text,
        created_at=datetime(2026, 11, 8, tzinfo=timezone.utc),
        referenced_tweets=[SimpleNamespace(type="replied_to", id=reply_to)] if reply_to else None,
        edit_history_tweet_ids=edits if edits is not None else [i])


def test_a_tweet_maps_to_a_public_post():
    p = ct.to_post(_tweet(101))
    assert (p.post_id, p.author_id, p.root_id, p.reply_to_id) == ("101", "42", "100", "100")
    assert p.created_at > 0 and p.edited is False
    assert ct.to_post(_tweet(105, edits=[101, 105])).edited is True
    root = ct.to_post(_tweet(100, conv="100", reply_to=None))
    assert root.root_id == root.post_id == "100" and root.reply_to_id == ""


def _tool(resp=None, refusal=None, me="999"):
    t = SimpleNamespace(_enabled=True, client=SimpleNamespace(get_users_mentions=MagicMock(),
                                                              get_tweet=MagicMock()))
    t.get_authenticated_user_id = AsyncMock(return_value=me)

    async def _req(func, endpoint_type, **kw):
        t.last = kw
        if refusal:
            raise RuntimeError(refusal)
        return resp
    t._make_request = _req
    return t


@pytest.mark.asyncio
async def test_poll_reads_after_the_cursor_oldest_first_and_is_throttled():
    resp = SimpleNamespace(data=[_tweet(103), _tweet(101)], meta={"newest_id": "103"})
    tool = _tool(resp)
    clock = [1000.0]
    ch = ct.XThreadChannel(lambda: tool, time_fn=lambda: clock[0])
    posts, newest = await ch.poll("90")
    assert [p.post_id for p in posts] == ["101", "103"] and newest == "103"
    assert tool.last["since_id"] == "90" and "conversation_id" in tool.last["tweet_fields"]
    assert "edit_history_tweet_ids" in tool.last["tweet_fields"]
    assert ch.agent_id() == "999"
    clock[0] += 10
    assert await ch.poll("103") == ([], "103")          # throttled: no read, no cost


@pytest.mark.asyncio
async def test_a_402_or_no_account_is_unavailable_with_a_reason():
    ch = ct.XThreadChannel(lambda: _tool(refusal="X API credits depleted (402 Payment Required)"))
    with pytest.raises(ThreadUnavailable, match="402"):
        await ch.poll(None)
    ch = ct.XThreadChannel(lambda: _tool(SimpleNamespace(data=[]), me=None))
    with pytest.raises(ThreadUnavailable, match="own account"):
        await ch.poll(None)


def test_code_writes_stay_inside_the_share(monkeypatch, tmp_path):
    monkeypatch.setattr(write_budget, "_db_path", lambda: str(tmp_path / "w.db"))
    monkeypatch.setenv("TWITTER_WRITE_MAX_PER_HOUR", "6")
    assert write_budget.share_available()                    # nothing used yet
    for _ in range(4):
        assert write_budget.reserve_write()
    assert not write_budget.share_available()                # 4 of 6: the code share is used
    assert write_budget.reserve_write()                      # the owner/model still have room


class _Replier:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    async def twitter_reply(self, params, execution_context=None):
        from polyrob_x.twitter_tool import _OWNER_APPROVED_ACTION
        self.calls.append((params.tweet_id, params.text, _OWNER_APPROVED_ACTION.get()))
        return SimpleNamespace(error=self.error)


@pytest.mark.asyncio
async def test_reply_is_the_owner_approved_action_and_is_recorded(monkeypatch):
    rec = []
    monkeypatch.setattr("core.effects.record_external_write", lambda **kw: rec.append(kw))
    monkeypatch.setattr("polyrob.sdk.is_owner_tenant", lambda ctx: True, raising=False)
    import polyrob.sdk as sdk
    monkeypatch.setattr(sdk, "is_owner_tenant", lambda ctx: True, raising=False)
    t = _Replier()
    ch = ct.XThreadChannel(lambda: t)
    ch._tool = t
    assert await ch.reply("101", "Order ord_1: pay", subject="101", user_id="owner")
    assert t.calls[0][0] == "101" and t.calls[0][2] == "twitter_reply"
    assert rec[0]["subject"] == "101" and rec[0]["outcome"] == "ok"
    assert rec[0]["action"] == "twitter_reply" and rec[0]["user_id"] == "owner"


@pytest.mark.asyncio
async def test_a_403_is_final_for_that_post(monkeypatch):
    monkeypatch.setattr("core.effects.record_external_write", lambda **kw: None)
    t = _Replier(error="403 Forbidden: You have not been mentioned or are not part of "
                       "the conversation thread")
    ch = ct.XThreadChannel(lambda: t)
    ch._tool = t
    assert await ch.reply("101", "x", user_id="") is False
    assert await ch.reply("101", "x", user_id="") is False
    assert len(t.calls) == 1                                # no second write burnt on it


def test_the_pack_registers_the_x_thread_channel():
    from polyrob_x import pack
    assert pack().hooks["commerce.thread_channel"] == {"x": "polyrob_x.commerce_thread:channel"}


@pytest.mark.asyncio
async def test_a_storefront_line_under_an_announcement_is_marked_on_mentions(monkeypatch):
    from polyrob_x.twitter_tool import _storefront_mark
    monkeypatch.setattr("core.commerce.thread.offer_of", lambda s, r, path=None: "art")
    assert "storefront" in _storefront_mark(_tweet(101, text="@me /buy wallet=0x1"))["answered_by"]
    assert _storefront_mark(_tweet(102, text="nice post")) == {}


@pytest.mark.asyncio
async def test_mentions_are_paged_back_to_the_cursor():
    pages = [SimpleNamespace(data=[_tweet(105)], meta={"newest_id": "105", "next_token": "t"}),
             SimpleNamespace(data=[_tweet(102)], meta={})]
    tool = _tool()
    calls = []

    async def _req(func, endpoint_type, **kw):
        calls.append(kw)
        return pages[len(calls) - 1]
    tool._make_request = _req
    ch = ct.XThreadChannel(lambda: tool)
    posts, newest = await ch.poll("100")
    assert [p.post_id for p in posts] == ["102", "105"] and newest == "105"
    assert calls[1]["pagination_token"] == "t"


def test_a_long_reply_reserves_one_unit_per_tweet():
    import inspect
    from polyrob_x import twitter_tool
    src = inspect.getsource(twitter_tool.TwitterTool._precheck_write)
    assert '("twitter_post", "twitter_reply")' in src


@pytest.mark.asyncio
async def test_an_overflow_reads_the_oldest_window_first_and_never_skips_it():
    """Over MAX_PAGES pages after the cursor: nothing is returned and the cursor stays;
    the next read pages the older part (until_id), and that window goes out first."""
    full = [SimpleNamespace(data=[_tweet(200 - i)], meta={"newest_id": "200", "next_token": "t"})
            for i in range(ct.MAX_PAGES)]
    gap = [SimpleNamespace(data=[_tweet(150), _tweet(120)], meta={"newest_id": "150"})]
    tool = _tool()
    calls = []

    async def _req(func, endpoint_type, **kw):
        calls.append(kw)
        return (gap if "until_id" in kw else full)[min(len(calls) - 1, ct.MAX_PAGES - 1)
                                                   if "until_id" not in kw else 0]
    tool._make_request = _req
    clock = [1000.0]
    ch = ct.XThreadChannel(lambda: tool, time_fn=lambda: clock[0])
    assert await ch.poll("100") == ([], "100")              # the cursor did not move
    clock[0] += ct.POLL_EVERY_S
    posts, newest = await ch.poll("100")
    assert calls[-1]["until_id"] == str(200 - ct.MAX_PAGES + 1)
    assert [p.post_id for p in posts] == ["120", "150"] and newest == "150"


@pytest.mark.asyncio
async def test_a_bare_403_does_not_kill_the_post_and_a_dead_post_expires(monkeypatch):
    monkeypatch.setattr("core.effects.record_external_write", lambda **kw: None)
    t = _Replier(error="403 Forbidden: client app is not permitted to perform this action")
    clock = [1000.0]
    ch = ct.XThreadChannel(lambda: t, time_fn=lambda: clock[0])
    ch._tool = t
    assert await ch.reply("101", "x", user_id="") is False
    assert await ch.reply("101", "x", user_id="") is False
    assert len(t.calls) == 2                                # retried: no fact about the post
    t.error = "403 Forbidden: You have not been mentioned or are not part of the conversation"
    assert await ch.reply("102", "x", user_id="") is False
    assert await ch.reply("102", "x", user_id="") is False and len(t.calls) == 3
    clock[0] += ct.DEAD_TTL_S
    assert await ch.reply("102", "x", user_id="") is False and len(t.calls) == 4
