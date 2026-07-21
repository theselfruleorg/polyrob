"""G1 — first-class FULL Twitter/X integration (write surface).

Mocks BOTH the v2 ``tweepy.Client`` and the v1.1 ``tweepy.API`` — no network. Verifies:
- write actions are registered only when TWITTER_ENABLED=true;
- each action calls the right client method with the right args;
- media upload (v1.1) → media_ids attached; threads chain in_reply_to; polls passed;
- text validation (>280 / empty) rejected at the param-model layer;
- approval gating (deny blocks, off proceeds) + per-class rate limits.
"""
import logging
from unittest.mock import MagicMock

import pytest

from tools.base_tool import ToolStatus
from tools.controller.execution_context import ActionExecutionContext
from tools.twitter_tool import (
    TwitterTool,
    TwitterPostAction,
    TwitterReplyAction,
    TwitterThreadAction,
)


@pytest.fixture
def _pm(tmp_path):
    """A real PathManager so media-path workspace resolution has somewhere to
    resolve against (mirrors tests/unit/tools/test_filesystem_write_verbatim.py)."""
    from agents.task.path import PathManager, set_path_manager
    pm = PathManager(data_root=str(tmp_path / "data"))
    set_path_manager(pm)
    return pm


def _ctx(session_id="s1", user_id="u1"):
    return ActionExecutionContext(session_id=session_id, user_id=user_id)


def _resp(tid="111", text="hi"):
    return MagicMock(data={"id": tid, "text": text})


def _tool(monkeypatch, *, enabled_env=True, require_approval=False):
    if enabled_env:
        monkeypatch.setenv("TWITTER_ENABLED", "true")
    else:
        monkeypatch.delenv("TWITTER_ENABLED", raising=False)
    monkeypatch.setenv("TWITTER_REQUIRE_APPROVAL", "true" if require_approval else "false")
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw-test")
    t.name = "twitter"
    t._status = ToolStatus.HEALTHY
    t._error_message = None
    t._enabled = True
    t._initialized = True
    t._container = MagicMock()
    t._services = {}
    t.client = MagicMock()         # v2 Client
    t.api_v1 = MagicMock()         # v1.1 API (media upload)
    t._write_times = []
    t._dm_times = []
    return t


# --- registration gating ---------------------------------------------------

def test_write_actions_absent_when_disabled(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    actions = t.get_actions()
    for name in ("twitter_post", "twitter_reply", "twitter_like", "twitter_dm",
                 "twitter_follow", "twitter_thread", "twitter_delete_tweet"):
        assert name not in actions, name
    # reads stay available regardless
    assert "twitter_search" in actions


def test_write_actions_present_when_enabled(monkeypatch):
    t = _tool(monkeypatch, enabled_env=True)
    actions = t.get_actions()
    for name in ("twitter_post", "twitter_reply", "twitter_quote", "twitter_thread",
                 "twitter_delete_tweet", "twitter_like", "twitter_unlike",
                 "twitter_retweet", "twitter_unretweet", "twitter_follow",
                 "twitter_unfollow", "twitter_dm"):
        assert name in actions, name


# --- param-model validation ------------------------------------------------

def test_post_text_over_280_accepted_for_autothread():
    # P6 (2026-07-02): >280 chars is no longer a param-model rejection — the tool
    # auto-splits into a thread (see test_twitter_autothread.py). Only the sanity
    # cap rejects.
    assert TwitterPostAction(text="x" * 281).text
    with pytest.raises(Exception):
        TwitterPostAction(text="x" * 20000)


def test_post_text_empty_rejected():
    with pytest.raises(Exception):
        TwitterPostAction(text="")


# --- compose: post / media / poll ------------------------------------------

@pytest.mark.asyncio
async def test_post_calls_create_tweet(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.return_value = _resp("900", "hello")
    res = await t.twitter_post(TwitterPostAction(text="hello"))
    assert res.error is None
    t.client.create_tweet.assert_called_once()
    assert t.client.create_tweet.call_args.kwargs["text"] == "hello"


@pytest.mark.asyncio
async def test_post_with_media_uploads_then_attaches(monkeypatch, _pm):
    t = _tool(monkeypatch)
    t.api_v1.media_upload.return_value = MagicMock(media_id=999)
    t.client.create_tweet.return_value = _resp()
    ws = _pm.get_workspace_dir("s1", "u1")
    (ws / "a.png").write_bytes(b"fake-png")
    res = await t.twitter_post(
        TwitterPostAction(text="pic", media_paths=["a.png"]), execution_context=_ctx()
    )
    assert res.error is None
    t.api_v1.media_upload.assert_called_once()
    assert t.client.create_tweet.call_args.kwargs["media_ids"] == ["999"]
    uploaded_path = t.api_v1.media_upload.call_args.kwargs["filename"]
    assert uploaded_path == str(ws / "a.png")


@pytest.mark.asyncio
async def test_post_with_media_collapses_container_workspace_prefix(monkeypatch, _pm):
    """The agent knows a file it just wrote inside the docker sandbox by the
    CONTAINER's own mount-point convention (`/workspace/foo.png`), not by the real
    host path — live prod hit exactly this ('No such file or directory:
    /workspace/video-first-post.mp4') even though the file existed and was
    readable via the filesystem tool moments earlier. Media upload must collapse
    that leading segment the same way FileSystem._normalize_path already does."""
    t = _tool(monkeypatch)
    t.api_v1.media_upload.return_value = MagicMock(media_id=999)
    t.client.create_tweet.return_value = _resp()
    ws = _pm.get_workspace_dir("s1", "u1")
    (ws / "video.mp4").write_bytes(b"fake-mp4")
    res = await t.twitter_post(
        TwitterPostAction(text="vid", media_paths=["/workspace/video.mp4"]),
        execution_context=_ctx(),
    )
    assert res.error is None
    uploaded_path = t.api_v1.media_upload.call_args.kwargs["filename"]
    assert uploaded_path == str(ws / "video.mp4")


@pytest.mark.asyncio
async def test_post_with_media_no_session_workspace_fails_cleanly(monkeypatch, _pm):
    """No resolvable session workspace (e.g. no execution_context) → a clean
    ActionResult error, never a bare tweepy FileNotFoundError from a raw path."""
    t = _tool(monkeypatch)
    res = await t.twitter_post(TwitterPostAction(text="pic", media_paths=["a.png"]))
    assert res.error is not None
    assert "workspace" in res.error
    t.api_v1.media_upload.assert_not_called()
    t.client.create_tweet.assert_not_called()


@pytest.mark.asyncio
async def test_post_with_media_fails_open_when_v1_client_missing(monkeypatch, _pm):
    """No v1.1 API client (no OAuth1.0a media path) → a media post returns a clean
    error ActionResult, never raises, and does NOT silently post text-only."""
    t = _tool(monkeypatch)
    t.api_v1 = None  # v1.1 media-upload client unavailable
    res = await t.twitter_post(
        TwitterPostAction(text="pic", media_paths=["/tmp/a.png"]), execution_context=_ctx()
    )
    assert res.error is not None  # surfaced cleanly, not an exception
    t.client.create_tweet.assert_not_called()  # upload failed before composing


@pytest.mark.asyncio
async def test_post_with_poll_passes_poll_kwargs(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.return_value = _resp()
    await t.twitter_post(TwitterPostAction(
        text="vote", poll_options=["a", "b"], poll_duration_minutes=60))
    kw = t.client.create_tweet.call_args.kwargs
    assert kw["poll_options"] == ["a", "b"] and kw["poll_duration_minutes"] == 60


# --- reply / thread / quote / delete ---------------------------------------

@pytest.mark.asyncio
async def test_reply_sets_in_reply_to(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.return_value = _resp()
    await t.twitter_reply(TwitterReplyAction(tweet_id="42", text="yo"))
    assert t.client.create_tweet.call_args.kwargs["in_reply_to_tweet_id"] == "42"


@pytest.mark.asyncio
async def test_thread_chains_in_reply_to(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("111"), _resp("222")]
    await t.twitter_thread(TwitterThreadAction(texts=["one", "two"]))
    assert t.client.create_tweet.call_count == 2
    second = t.client.create_tweet.call_args_list[1].kwargs
    assert second["in_reply_to_tweet_id"] == "111"


@pytest.mark.asyncio
async def test_delete_tweet_calls_client(monkeypatch):
    from tools.twitter_tool import TwitterDeleteAction
    t = _tool(monkeypatch)
    t.client.delete_tweet.return_value = MagicMock(data={"deleted": True})
    await t.twitter_delete_tweet(TwitterDeleteAction(tweet_id="55"))
    t.client.delete_tweet.assert_called_once()
    assert t.client.delete_tweet.call_args.kwargs.get("id") == "55"


# --- engagement ------------------------------------------------------------

@pytest.mark.asyncio
async def test_like_calls_client_like(monkeypatch):
    from tools.twitter_tool import TwitterTweetIdAction
    t = _tool(monkeypatch)
    t.client.like.return_value = MagicMock(data={"liked": True})
    await t.twitter_like(TwitterTweetIdAction(tweet_id="7"))
    t.client.like.assert_called_once()


@pytest.mark.asyncio
async def test_retweet_calls_client_retweet(monkeypatch):
    from tools.twitter_tool import TwitterTweetIdAction
    t = _tool(monkeypatch)
    t.client.retweet.return_value = MagicMock(data={"retweeted": True})
    await t.twitter_retweet(TwitterTweetIdAction(tweet_id="7"))
    t.client.retweet.assert_called_once()
    assert t.client.retweet.call_args.kwargs.get("tweet_id") == "7"


@pytest.mark.asyncio
async def test_unretweet_uses_source_tweet_id_kwarg(monkeypatch):
    """tweepy's unretweet takes ``source_tweet_id`` (not ``tweet_id``) — lock it."""
    from tools.twitter_tool import TwitterTweetIdAction
    t = _tool(monkeypatch)
    t.client.unretweet.return_value = MagicMock(data={"retweeted": False})
    await t.twitter_unretweet(TwitterTweetIdAction(tweet_id="7"))
    t.client.unretweet.assert_called_once()
    assert t.client.unretweet.call_args.kwargs.get("source_tweet_id") == "7"


# --- relationship + DM (numeric ids skip resolution) -----------------------

@pytest.mark.asyncio
async def test_follow_calls_client_follow(monkeypatch):
    from tools.twitter_tool import TwitterUserAction
    t = _tool(monkeypatch)
    t.client.follow_user.return_value = MagicMock(data={"following": True})
    await t.twitter_follow(TwitterUserAction(user="123456"))
    t.client.follow_user.assert_called_once()
    assert t.client.follow_user.call_args.kwargs.get("target_user_id") == "123456"


@pytest.mark.asyncio
async def test_dm_calls_create_direct_message(monkeypatch):
    from tools.twitter_tool import TwitterDMAction
    t = _tool(monkeypatch)
    t.client.create_direct_message.return_value = MagicMock(data={"dm_conversation_id": "c1"})
    await t.twitter_dm(TwitterDMAction(recipient="123456", text="hi there"))
    t.client.create_direct_message.assert_called_once()
    assert t.client.create_direct_message.call_args.kwargs.get("participant_id") == "123456"


# --- approval gating -------------------------------------------------------

@pytest.mark.asyncio
async def test_approval_deny_blocks_write(monkeypatch):
    monkeypatch.setenv("APPROVAL_PROVIDER", "deny")
    t = _tool(monkeypatch, require_approval=True)
    t.client.create_tweet.return_value = _resp()
    res = await t.twitter_post(TwitterPostAction(text="blocked?"))
    assert res.error is not None and "approval" in res.error.lower()
    t.client.create_tweet.assert_not_called()


@pytest.mark.asyncio
async def test_approval_off_proceeds(monkeypatch):
    t = _tool(monkeypatch, require_approval=False)
    t.client.create_tweet.return_value = _resp()
    res = await t.twitter_post(TwitterPostAction(text="ok"))
    assert res.error is None
    t.client.create_tweet.assert_called_once()


# --- rate limiting ---------------------------------------------------------

@pytest.mark.asyncio
async def test_write_rate_limit_trips(monkeypatch):
    monkeypatch.setenv("TWITTER_WRITE_MAX_PER_HOUR", "2")
    t = _tool(monkeypatch, require_approval=False)
    t.client.create_tweet.return_value = _resp()
    assert (await t.twitter_post(TwitterPostAction(text="1"))).error is None
    assert (await t.twitter_post(TwitterPostAction(text="2"))).error is None
    third = await t.twitter_post(TwitterPostAction(text="3"))
    assert third.error is not None and "rate" in third.error.lower()
    assert t.client.create_tweet.call_count == 2


@pytest.mark.asyncio
async def test_dm_rate_limit_independent(monkeypatch):
    from tools.twitter_tool import TwitterDMAction
    monkeypatch.setenv("TWITTER_DM_MAX_PER_HOUR", "1")
    t = _tool(monkeypatch, require_approval=False)
    t.client.create_direct_message.return_value = MagicMock(data={"dm_conversation_id": "c"})
    assert (await t.twitter_dm(TwitterDMAction(recipient="1", text="a"))).error is None
    second = await t.twitter_dm(TwitterDMAction(recipient="1", text="b"))
    assert second.error is not None and "rate" in second.error.lower()


# --- X-capability completion: unmute, DM reads, timeline reads ---------------


@pytest.mark.asyncio
async def test_unmute_gated_and_calls_client(monkeypatch):
    t = _tool(monkeypatch, enabled_env=True)
    assert "twitter_unmute" in t.get_actions()
    from tools.twitter_tool import TwitterUserAction
    res = await t.twitter_unmute(TwitterUserAction(user="123456"))
    assert res.error is None
    t.client.unmute.assert_called_once_with(target_user_id="123456")


def test_unmute_absent_when_writes_disabled(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    assert "twitter_unmute" not in t.get_actions()


def test_dm_and_timeline_reads_always_available(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    actions = t.get_actions()
    assert "twitter_get_dms" in actions
    assert "twitter_get_timeline" in actions


@pytest.mark.asyncio
async def test_get_dms_lists_events(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    ev = MagicMock()
    ev.data = {"id": "9001", "event_type": "MessageCreate", "text": "yo",
               "sender_id": "42", "dm_conversation_id": "42-999",
               "created_at": "2026-07-12T00:00:00.000Z"}
    t.client.get_direct_message_events = MagicMock(
        return_value=MagicMock(data=[ev]))
    from tools.twitter_tool import TwitterGetDMsAction
    res = await t.twitter_get_dms(TwitterGetDMsAction())
    assert res.error is None
    assert "yo" in res.extracted_content
    assert "42-999" in res.extracted_content
    kwargs = t.client.get_direct_message_events.call_args.kwargs
    assert kwargs["event_types"] == "MessageCreate"
    assert "participant_id" not in kwargs


@pytest.mark.asyncio
async def test_get_dms_participant_filter(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    t.client.get_direct_message_events = MagicMock(
        return_value=MagicMock(data=[]))
    from tools.twitter_tool import TwitterGetDMsAction
    res = await t.twitter_get_dms(
        TwitterGetDMsAction(participant="123456", max_results=5))
    assert res.error is None
    kwargs = t.client.get_direct_message_events.call_args.kwargs
    assert kwargs["participant_id"] == "123456"
    assert kwargs["max_results"] == 5


@pytest.mark.asyncio
async def test_get_timeline_renders_tweets(monkeypatch):
    from types import SimpleNamespace

    t = _tool(monkeypatch, enabled_env=False)
    tweet = SimpleNamespace(id="77", text="hello world", created_at=None,
                            public_metrics={"like_count": 3})
    t.client.get_users_tweets = MagicMock(return_value=MagicMock(data=[tweet]))
    from tools.twitter_tool import TwitterTimelineAction
    res = await t.twitter_get_timeline(TwitterTimelineAction(user="123456"))
    assert res.error is None
    assert "hello world" in res.extracted_content
    call_kwargs = t.client.get_users_tweets.call_args.kwargs
    assert call_kwargs["id"] == "123456"


def test_media_paths_description_warns_against_live_debug_posts():
    """2026-07-20: 13+ debug-scratch posts ("Testing Twitter media upload with
    absolute path", etc.) leaked onto the live public account while a session
    iterated on a media_paths bug (docs/ops/inbox.md, ~10:12Z). There's no staging
    account, so the fix is a schema-level steer: the LLM sees this description on
    every media_paths field before it ever calls the action."""
    for cls in (TwitterPostAction, TwitterReplyAction, TwitterThreadAction):
        desc = cls.model_fields["media_paths"].description
        assert "LIVE public account" in desc
        assert "verify" in desc.lower()
