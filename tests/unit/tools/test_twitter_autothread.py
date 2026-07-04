"""P6 (2026-07-02): over-280-char post/reply text auto-splits into a thread.

Prod evidence (journal 2026-07-01 22:13): the agent repeatedly composed replies
longer than 280 chars; TwitterReplyAction's max_length=280 rejected them at the
registry validation layer, the call was skipped, and 'engage on X' goals
completed having posted NOTHING. LLMs cannot count characters reliably, so a
hard param-model cap turns every long composition into a dead action. The tool
now accepts longer text and does what a human would do — split at word
boundaries with (i/n) counters and post a chained thread. 280 stays the hard
per-tweet wire limit.
"""
import logging
from unittest.mock import MagicMock

import pytest

from tools.base_tool import ToolStatus
from tools.twitter_tool import (
    TwitterTool,
    TwitterPostAction,
    TwitterReplyAction,
)


def _resp_seq(*ids):
    return [MagicMock(data={"id": i, "text": "t"}) for i in ids]


def _tool(monkeypatch):
    monkeypatch.setenv("TWITTER_ENABLED", "true")
    monkeypatch.setenv("TWITTER_REQUIRE_APPROVAL", "false")
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw-test")
    t.name = "twitter"
    t._status = ToolStatus.HEALTHY
    t._error_message = None
    t._enabled = True
    t._initialized = True
    t._container = MagicMock()
    t._services = {}
    t.client = MagicMock()
    t.api_v1 = MagicMock()
    t._write_times = []
    t._dm_times = []
    return t


LONG = ("POLYROB is an enterprise-grade AI automation platform with durable "
        "goals, multi-agent coordination and a terminal-native CLI. ") * 5  # ~600 chars


# --- the splitter ------------------------------------------------------------

def test_split_short_text_is_identity():
    assert TwitterTool._split_tweet_text("hello world") == ["hello world"]


def test_split_long_text_parts_fit_and_are_numbered():
    parts = TwitterTool._split_tweet_text(LONG)
    assert len(parts) >= 2
    for i, p in enumerate(parts, 1):
        assert len(p) <= 280, f"part {i} too long: {len(p)}"
        assert f"({i}/{len(parts)})" in p


def test_split_preserves_all_words():
    parts = TwitterTool._split_tweet_text(LONG)
    stripped = []
    for i, p in enumerate(parts, 1):
        assert p.endswith(f"({i}/{len(parts)})")
        stripped.append(p[: p.rfind("(")].strip())
    assert " ".join(stripped).split() == LONG.split()


def test_split_hard_breaks_pathological_word():
    parts = TwitterTool._split_tweet_text("x" * 900)
    assert all(len(p) <= 280 for p in parts)


# --- param models accept long text (validation no longer kills the action) ---

def test_reply_action_accepts_long_text():
    a = TwitterReplyAction(tweet_id="1", text="y" * 600)
    assert len(a.text) == 600


def test_post_action_accepts_long_text():
    a = TwitterPostAction(text="y" * 600)
    assert len(a.text) == 600


def test_post_action_still_rejects_absurd_text():
    with pytest.raises(Exception):
        TwitterPostAction(text="y" * 20000)


# --- reply auto-threads ------------------------------------------------------

@pytest.mark.asyncio
async def test_long_reply_posts_chained_thread(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = _resp_seq("801", "802", "803", "804")
    res = await t.twitter_reply(TwitterReplyAction(tweet_id="500", text=LONG))
    assert res.error is None

    calls = t.client.create_tweet.call_args_list
    assert len(calls) >= 2
    # first part replies to the TARGET tweet
    assert calls[0].kwargs["in_reply_to_tweet_id"] == "500"
    # subsequent parts chain to the previously created tweet
    assert calls[1].kwargs["in_reply_to_tweet_id"] == "801"
    for c in calls:
        assert len(c.kwargs["text"]) <= 280


@pytest.mark.asyncio
async def test_short_reply_stays_single_tweet(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.return_value = _resp_seq("801")[0]
    res = await t.twitter_reply(TwitterReplyAction(tweet_id="500", text="short reply"))
    assert res.error is None
    assert t.client.create_tweet.call_count == 1


# --- post auto-threads -------------------------------------------------------

@pytest.mark.asyncio
async def test_long_post_posts_chained_thread(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = _resp_seq("901", "902", "903", "904")
    res = await t.twitter_post(TwitterPostAction(text=LONG))
    assert res.error is None

    calls = t.client.create_tweet.call_args_list
    assert len(calls) >= 2
    assert "in_reply_to_tweet_id" not in calls[0].kwargs  # head tweet
    assert calls[1].kwargs["in_reply_to_tweet_id"] == "901"
    for c in calls:
        assert len(c.kwargs["text"]) <= 280
