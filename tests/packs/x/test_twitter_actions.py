"""G1 — first-class FULL Twitter/X integration (write surface).

Mocks BOTH the v2 ``tweepy.Client`` and the v1.1 ``tweepy.API`` — no network. Verifies:
- write actions are registered only when TWITTER_ENABLED=true;
- each action calls the right client method with the right args;
- media upload (v1.1) → media_ids attached; threads chain in_reply_to; polls passed;
- text validation (>280 / empty) rejected at the param-model layer;
- approval gating (deny blocks, off proceeds) + per-class rate limits.
"""
import pytest

pytest.importorskip("polyrob_x")

import logging
from unittest.mock import MagicMock

import pytest

from tools.base_tool import ToolStatus
from tools.controller.execution_context import ActionExecutionContext
from polyrob_x.twitter_tool import (
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
    # The OWNER's interactive turn (u1 is the owner principal in `_tool`).
    return ActionExecutionContext(session_id=session_id, user_id=user_id, role="orchestrator")


def _resp(tid="111", text="hi"):
    return MagicMock(data={"id": tid, "text": text})


def _tool(monkeypatch, *, enabled_env=True, require_approval=False):
    if enabled_env:
        monkeypatch.setenv("TWITTER_ENABLED", "true")
    else:
        monkeypatch.delenv("TWITTER_ENABLED", raising=False)
    monkeypatch.setenv("TWITTER_REQUIRE_APPROVAL", "true" if require_approval else "false")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
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
    # Image path: no video-only kwargs added.
    assert "chunked" not in t.api_v1.media_upload.call_args.kwargs
    assert "media_category" not in t.api_v1.media_upload.call_args.kwargs


@pytest.mark.asyncio
async def test_post_with_video_media_uses_chunked_upload_with_category(monkeypatch, _pm):
    """2026-07-19 HIGH item: bare media_upload(filename=...) never told X this
    was a tweet-attachable video. tweepy already auto-chunks + waits for
    processing on video (chunked_upload defaults wait_for_async_finalize=True)
    — the missing piece was media_category="tweet_video"."""
    t = _tool(monkeypatch)
    t.api_v1.media_upload.return_value = MagicMock(media_id=888)
    t.client.create_tweet.return_value = _resp()
    ws = _pm.get_workspace_dir("s1", "u1")
    (ws / "clip.mp4").write_bytes(b"fake-mp4")
    res = await t.twitter_post(
        TwitterPostAction(text="video!", media_paths=["clip.mp4"]), execution_context=_ctx()
    )
    assert res.error is None
    t.api_v1.media_upload.assert_called_once()
    kwargs = t.api_v1.media_upload.call_args.kwargs
    assert kwargs["chunked"] is True
    assert kwargs["media_category"] == "tweet_video"
    assert t.client.create_tweet.call_args.kwargs["media_ids"] == ["888"]


@pytest.mark.asyncio
async def test_post_with_mov_media_also_uses_chunked_upload(monkeypatch, _pm):
    t = _tool(monkeypatch)
    t.api_v1.media_upload.return_value = MagicMock(media_id=777)
    t.client.create_tweet.return_value = _resp()
    ws = _pm.get_workspace_dir("s1", "u1")
    (ws / "clip.MOV").write_bytes(b"fake-mov")
    await t.twitter_post(
        TwitterPostAction(text="video!", media_paths=["clip.MOV"]), execution_context=_ctx()
    )
    kwargs = t.api_v1.media_upload.call_args.kwargs
    assert kwargs["media_category"] == "tweet_video"  # case-insensitive extension match


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
    from polyrob_x.twitter_tool import TwitterDeleteAction
    t = _tool(monkeypatch)
    t.client.delete_tweet.return_value = MagicMock(data={"deleted": True})
    await t.twitter_delete_tweet(TwitterDeleteAction(tweet_id="55"))
    t.client.delete_tweet.assert_called_once()
    assert t.client.delete_tweet.call_args.kwargs.get("id") == "55"


# --- engagement ------------------------------------------------------------

@pytest.mark.asyncio
async def test_like_calls_client_like(monkeypatch):
    from polyrob_x.twitter_tool import TwitterTweetIdAction
    t = _tool(monkeypatch)
    t.client.like.return_value = MagicMock(data={"liked": True})
    await t.twitter_like(TwitterTweetIdAction(tweet_id="7"))
    t.client.like.assert_called_once()


@pytest.mark.asyncio
async def test_retweet_calls_client_retweet(monkeypatch):
    from polyrob_x.twitter_tool import TwitterTweetIdAction
    t = _tool(monkeypatch)
    t.client.retweet.return_value = MagicMock(data={"retweeted": True})
    await t.twitter_retweet(TwitterTweetIdAction(tweet_id="7"))
    t.client.retweet.assert_called_once()
    assert t.client.retweet.call_args.kwargs.get("tweet_id") == "7"


@pytest.mark.asyncio
async def test_unretweet_uses_source_tweet_id_kwarg(monkeypatch):
    """tweepy's unretweet takes ``source_tweet_id`` (not ``tweet_id``) — lock it."""
    from polyrob_x.twitter_tool import TwitterTweetIdAction
    t = _tool(monkeypatch)
    t.client.unretweet.return_value = MagicMock(data={"retweeted": False})
    await t.twitter_unretweet(TwitterTweetIdAction(tweet_id="7"))
    t.client.unretweet.assert_called_once()
    assert t.client.unretweet.call_args.kwargs.get("source_tweet_id") == "7"


# --- relationship + DM (numeric ids skip resolution) -----------------------

@pytest.mark.asyncio
async def test_follow_calls_client_follow(monkeypatch):
    from polyrob_x.twitter_tool import TwitterUserAction
    t = _tool(monkeypatch)
    t.client.follow_user.return_value = MagicMock(data={"following": True})
    await t.twitter_follow(TwitterUserAction(user="123456"))
    t.client.follow_user.assert_called_once()
    assert t.client.follow_user.call_args.kwargs.get("target_user_id") == "123456"


@pytest.mark.asyncio
async def test_dm_calls_create_direct_message(monkeypatch):
    from polyrob_x.twitter_tool import TwitterDMAction
    t = _tool(monkeypatch)
    t.client.create_direct_message.return_value = MagicMock(data={"dm_conversation_id": "c1"})
    await t.twitter_dm(TwitterDMAction(recipient="123456", text="hi there", allow_plaintext=True))
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
    from polyrob_x.twitter_tool import TwitterDMAction
    monkeypatch.setenv("TWITTER_DM_MAX_PER_HOUR", "1")
    t = _tool(monkeypatch, require_approval=False)
    t.client.create_direct_message.return_value = MagicMock(data={"dm_conversation_id": "c"})
    assert (await t.twitter_dm(TwitterDMAction(recipient="1", text="a", allow_plaintext=True))).error is None
    second = await t.twitter_dm(TwitterDMAction(recipient="1", text="b", allow_plaintext=True))
    assert second.error is not None and "rate" in second.error.lower()


# --- X-capability completion: unmute, DM reads, timeline reads ---------------


@pytest.mark.asyncio
async def test_unmute_gated_and_calls_client(monkeypatch):
    t = _tool(monkeypatch, enabled_env=True)
    assert "twitter_unmute" in t.get_actions()
    from polyrob_x.twitter_tool import TwitterUserAction
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
        return_value=MagicMock(data=[ev], meta={"next_token": "next-1"}))
    from polyrob_x.twitter_tool import TwitterGetDMsAction
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="legacy"))
    assert res.error is None
    assert "yo" in res.extracted_content
    assert "42-999" in res.extracted_content
    assert '"next_token": "next-1"' in res.extracted_content
    assert "cannot show any recent inbound message" in res.extracted_content
    kwargs = t.client.get_direct_message_events.call_args.kwargs
    assert kwargs["event_types"] == "MessageCreate"
    assert "participant_id" not in kwargs


@pytest.mark.asyncio
async def test_get_dms_participant_filter(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    t.client.get_direct_message_events = MagicMock(
        return_value=MagicMock(data=[]))
    from polyrob_x.twitter_tool import TwitterGetDMsAction
    res = await t.twitter_get_dms(
        TwitterGetDMsAction(participant="123456", max_results=5))
    assert res.error is None
    kwargs = t.client.get_direct_message_events.call_args.kwargs
    assert kwargs["participant_id"] == "123456"
    assert kwargs["max_results"] == 5


@pytest.mark.asyncio
async def test_get_dms_conversation_route_and_pagination(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    t.client.get_direct_message_events = MagicMock(
        return_value=MagicMock(data=[], meta={"previous_token": "prev"}))
    from polyrob_x.twitter_tool import TwitterGetDMsAction
    res = await t.twitter_get_dms(TwitterGetDMsAction(
        conversation_id="42-999", pagination_token="page-2"))
    assert res.error is None
    kwargs = t.client.get_direct_message_events.call_args.kwargs
    assert kwargs["dm_conversation_id"] == "42-999"
    assert kwargs["pagination_token"] == "page-2"
    assert "participant_id" not in kwargs
    assert '"previous_token": "prev"' in res.extracted_content


@pytest.mark.asyncio
async def test_get_dms_rejects_ambiguous_scope(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    from polyrob_x.twitter_tool import TwitterGetDMsAction
    res = await t.twitter_get_dms(TwitterGetDMsAction(
        participant="42", conversation_id="42-999"))
    assert res.error and "not both" in res.error
    t.client.get_direct_message_events.assert_not_called()


@pytest.mark.asyncio
async def test_get_dms_empty_is_not_reported_as_no_replies(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    t.client.get_direct_message_events = MagicMock(
        return_value=MagicMock(data=[], meta={}))
    from polyrob_x.twitter_tool import TwitterGetDMsAction
    res = await t.twitter_get_dms(TwitterGetDMsAction())
    assert res.error is None
    assert '"events": []' in res.extracted_content
    assert "cannot show any recent inbound message" in res.extracted_content
    assert "no replies" not in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_get_dms_uses_oauth2_user_client(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    t.oauth2_access_token = "oauth2-user-token"
    t.dm_client = MagicMock()
    t.dm_client.get_direct_message_events.return_value = MagicMock(data=[], meta={})
    from polyrob_x.twitter_tool import TwitterGetDMsAction
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="legacy"))
    assert res.error is None
    t.client.get_direct_message_events.assert_not_called()
    kwargs = t.dm_client.get_direct_message_events.call_args.kwargs
    assert kwargs["user_auth"] is False


@pytest.mark.asyncio
async def test_get_dms_auto_prefers_encrypted_chat_with_oauth2(monkeypatch):
    from unittest.mock import AsyncMock
    from polyrob_x.twitter_tool import TwitterGetDMsAction

    t = _tool(monkeypatch, enabled_env=False)
    t.oauth2_access_token = "oauth2-user-token"
    t.chat_client = MagicMock()
    t.chat_client.get_conversations = AsyncMock(return_value={
        "data": [{"id": "42-999", "participant_ids": ["42", "999"]}],
        "includes": {"users": [{"id": "42", "username": "friend"}]},
        "meta": {"next_token": "chat-next"},
    })

    res = await t.twitter_get_dms(TwitterGetDMsAction())
    assert res.error is None
    assert '"rail": "chat"' in res.extracted_content
    assert "friend" in res.extracted_content
    t.client.get_direct_message_events.assert_not_called()


@pytest.mark.asyncio
async def test_get_dms_chat_thread_surfaces_decryption_status(monkeypatch):
    from unittest.mock import AsyncMock
    from polyrob_x.twitter_tool import TwitterGetDMsAction

    t = _tool(monkeypatch, enabled_env=False)
    t.oauth2_access_token = "oauth2-user-token"
    t.chat_client = MagicMock()
    t.chat_client.read_conversation = AsyncMock(return_value={
        "conversation_id": "42",
        "events": [{"id": "e1", "encoded_event": "ciphertext"}],
        "decryption": {"status": "not_configured"},
    })

    res = await t.twitter_get_dms(TwitterGetDMsAction(
        participant="42", rail="chat"))
    assert res.error is None
    assert '"status": "not_configured"' in res.extracted_content
    t.chat_client.read_conversation.assert_awaited_once_with(
        "42", participant_ids=["42"], max_results=20,
        pagination_token=None)


@pytest.mark.asyncio
async def test_get_timeline_renders_tweets(monkeypatch):
    from types import SimpleNamespace

    t = _tool(monkeypatch, enabled_env=False)
    tweet = SimpleNamespace(id="77", text="hello world", created_at=None,
                            public_metrics={"like_count": 3})
    t.client.get_users_tweets = MagicMock(return_value=MagicMock(data=[tweet]))
    from polyrob_x.twitter_tool import TwitterTimelineAction
    res = await t.twitter_get_timeline(TwitterTimelineAction(user="123456"))
    assert res.error is None
    assert "hello world" in res.extracted_content
    call_kwargs = t.client.get_users_tweets.call_args.kwargs
    assert call_kwargs["id"] == "123456"


def test_media_paths_description_warns_against_live_debug_posts():
    """2026-07-20: 13+ debug-scratch posts ("Testing Twitter media upload with
    absolute path", etc.) leaked onto the live public account while a session
    iterated on a media_paths bug (~10:12Z). There's no staging
    account, so the fix is a schema-level steer: the LLM sees this description on
    every media_paths field before it ever calls the action."""
    for cls in (TwitterPostAction, TwitterReplyAction, TwitterThreadAction):
        desc = cls.model_fields["media_paths"].description
        assert "LIVE public account" in desc
        assert "verify" in desc.lower()


# --- poll results (read) ----------------------------------------------------

def _poll_resp(options, status="closed"):
    poll = MagicMock()
    poll.options = options
    poll.voting_status = status
    poll.end_datetime = None
    poll.duration_minutes = 60
    resp = MagicMock()
    resp.data = MagicMock(text="what next?")
    resp.includes = {"polls": [poll]}
    return resp


@pytest.mark.asyncio
async def test_poll_results_reads_attachment_and_ranks_options(monkeypatch):
    """A poll is an ATTACHMENT: the read must ask for `attachments.poll_ids` +
    `poll_fields`, and the result must rank options by votes with shares."""
    from polyrob_x.twitter_tool import TwitterPollResultsAction
    t = _tool(monkeypatch)

    async def _mk(func, endpoint_type, **kw):
        assert "attachments.poll_ids" in kw["expansions"]
        assert "options" in kw["poll_fields"] and "voting_status" in kw["poll_fields"]
        return _poll_resp([{"position": 1, "label": "buy", "votes": 3},
                           {"position": 2, "label": "wait", "votes": 9}])
    t._make_request = _mk
    res = await t.twitter_poll_results(TwitterPollResultsAction(tweet_id="777"))
    assert res.error is None
    assert '"total_votes": 12' in res.extracted_content
    body = res.extracted_content
    assert body.index('"label": "wait"') < body.index('"label": "buy"')
    assert '"share_pct": 75.0' in body


@pytest.mark.asyncio
async def test_poll_results_names_a_tweet_without_a_poll(monkeypatch):
    from polyrob_x.twitter_tool import TwitterPollResultsAction
    t = _tool(monkeypatch)

    async def _mk(func, endpoint_type, **kw):
        resp = MagicMock(); resp.data = MagicMock(text="plain"); resp.includes = {}
        return resp
    t._make_request = _mk
    res = await t.twitter_poll_results(TwitterPollResultsAction(tweet_id="1"))
    assert res.error and "no poll" in res.error


def test_poll_results_is_a_read_available_when_writes_are_off(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    assert "twitter_poll_results" in t.get_actions()



# --- AGT-7/SUP-6: who may write ----------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["auto", "auto_notify", ""])
async def test_autonomous_write_needs_a_real_owner_approval(monkeypatch, provider):
    """An automatic provider never approves a non-owner turn, and
    TWITTER_REQUIRE_APPROVAL=false does not skip the gate for it."""
    monkeypatch.setenv("APPROVAL_PROVIDER", provider)
    t = _tool(monkeypatch, require_approval=False)
    asked = []

    class _Queue:
        decides_as_owner = True

        async def request(self, name, params, ctx):
            asked.append(name)
            return False

    import tools.controller.approval as approval
    real = approval.get_approval_provider_or_deny
    monkeypatch.setattr(approval, "get_approval_provider_or_deny",
                        lambda name, **k: _Queue() if name == "owner_queue" else real(name, **k))
    leaf = ActionExecutionContext(session_id="s1", user_id="u1")       # autonomous shape
    res = await t.twitter_post(TwitterPostAction(text="x"), execution_context=leaf)
    assert res.error and "approval denied" in res.error
    assert asked == ["twitter_post"]
    t.client.create_tweet.assert_not_called()


@pytest.mark.asyncio
async def test_tainted_owner_turn_needs_approval(monkeypatch):
    t = _tool(monkeypatch, require_approval=False)
    ctx = ActionExecutionContext(session_id="s1", user_id="u1", role="orchestrator")
    ctx.metadata = {"untrusted_read": True}
    import tools.controller.approval as approval
    monkeypatch.setattr(approval, "get_approval_provider_or_deny",
                        lambda name, **k: approval.DenyByDefaultApprover())
    res = await t.twitter_post(TwitterPostAction(text="x"), execution_context=ctx)
    assert res.error and "approval" in res.error.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("require", [True, False])
async def test_owner_turn_posts_with_no_provider_configured(monkeypatch, require):
    """Prod shape: APPROVAL_PROVIDER unset. The owner asking IS the approval."""
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    t = _tool(monkeypatch, require_approval=require)
    t.client.create_tweet.return_value = _resp()
    res = await t.twitter_post(TwitterPostAction(text="hello"), execution_context=_ctx())
    assert res.error is None
    t.client.create_tweet.assert_called_once()


# --- an OWNER-authored standing cron job posts as its author wrote it ---------

def _cron_ctx(monkeypatch, sid, job_id, *, owner_job=True, tainted=False,
              sub_agent=False):
    from agents.task.goals import autonomy_marker as am
    am.mark_autonomous(sid, cron_job_id=job_id)
    if owner_job:
        am.note_owner_job(job_id, "Daily 09:00: post the buyback notice on X")
    ctx = ActionExecutionContext(session_id=sid, user_id="u1",
                                 role="leaf" if sub_agent else "orchestrator")
    ctx.metadata = {"untrusted_read": True} if tainted else {}
    return ctx


def _owner_queue(monkeypatch):
    asked = []

    class _Queue:
        decides_as_owner = True

        async def request(self, name, params, ctx):
            asked.append(name)
            return False

    import tools.controller.approval as approval
    real = approval.get_approval_provider_or_deny
    monkeypatch.setattr(approval, "get_approval_provider_or_deny",
                        lambda name, **k: _Queue() if name == "owner_queue" else real(name, **k))
    return asked


@pytest.mark.asyncio
async def test_owner_authored_standing_cron_posts_without_a_queue(monkeypatch):
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    t = _tool(monkeypatch, require_approval=False)
    asked = _owner_queue(monkeypatch)
    t.client.create_tweet.return_value = _resp()
    ctx = _cron_ctx(monkeypatch, "cron-buyback-1", "job-buyback")
    res = await t.twitter_post(TwitterPostAction(text="Buyback done"), execution_context=ctx)
    assert res.error is None and asked == []
    t.client.create_tweet.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["agent_authored", "tainted", "sub_agent"])
async def test_agent_authored_tainted_or_delegated_cron_still_needs_the_owner(monkeypatch, shape):
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    t = _tool(monkeypatch, require_approval=False)
    asked = _owner_queue(monkeypatch)
    ctx = _cron_ctx(monkeypatch, f"cron-{shape}", f"job-{shape}",
                    owner_job=(shape != "agent_authored"),
                    tainted=(shape == "tainted"), sub_agent=(shape == "sub_agent"))
    res = await t.twitter_post(TwitterPostAction(text="x"), execution_context=ctx)
    assert res.error and "approval denied" in res.error
    assert asked == ["twitter_post"]
    t.client.create_tweet.assert_not_called()


# --- prod 2026-10-08 12:05 (buyback 4def3260d811): the REAL owner queue ---------
# APPROVAL_PROVIDER unset, TWITTER_REQUIRE_APPROVAL=false, an agent-authored
# autonomous cron run. The queue created the ask and returned at once ("the
# next run redeems the grant" — never, for a post: its text is new each run),
# and the tool said "approval denied". The post now WAITS for the tap.

def _real_queue(monkeypatch, tmp_path, wait_s=0.6):
    from agents.task.goals.board import GoalBoard
    from tools.controller import approval_queue as aq
    import tools.controller.approval as approval
    board = GoalBoard(str(tmp_path / "goals.db"))
    pushed = []

    async def _push(container, user_id, text):
        pushed.append(text)
    monkeypatch.setattr(aq, "_push_owner_notification", _push)
    queue = aq.OwnerQueueApprover(user_id="u1", board=board, poll_interval=0.05)
    real = approval.get_approval_provider_or_deny
    monkeypatch.setattr(approval, "get_approval_provider_or_deny",
                        lambda name, **k: queue if name == "owner_queue" else real(name, **k))
    monkeypatch.setattr(approval, "approval_wait_timeout_sec", lambda *a, **k: wait_s)
    return board, pushed


@pytest.mark.asyncio
async def test_prod_shape_agent_cron_post_asks_and_says_it_is_waiting(monkeypatch, tmp_path):
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    t = _tool(monkeypatch, require_approval=False)
    board, pushed = _real_queue(monkeypatch, tmp_path)
    ctx = _cron_ctx(monkeypatch, "cron-prod-1", "job-4def", owner_job=False)
    res = await t.twitter_post(TwitterPostAction(text="Bought 0.05 ETH of PNL."),
                               execution_context=ctx)
    assert res.error and "waiting for the owner's approval" in res.error
    assert "denied" not in res.error
    asks = board.asks(user_id="u1", status="open")
    assert len(asks) == 1 and "twitter_post" in asks[0].title
    assert pushed, "the owner gets the card"
    t.client.create_tweet.assert_not_called()


@pytest.mark.asyncio
async def test_prod_shape_agent_cron_post_goes_out_when_the_owner_taps(monkeypatch, tmp_path):
    import asyncio as _asyncio
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    t = _tool(monkeypatch, require_approval=False)
    t.client.create_tweet.return_value = _resp()
    board, _pushed = _real_queue(monkeypatch, tmp_path, wait_s=5.0)
    ctx = _cron_ctx(monkeypatch, "cron-prod-2", "job-4def-b", owner_job=False)

    async def _owner_taps():
        for _ in range(100):
            open_asks = board.asks(user_id="u1", status="open")
            if open_asks:
                board.decide_ask(open_asks[0].id, user_id="u1", approved=True)
                return
            await _asyncio.sleep(0.02)

    tap = _asyncio.create_task(_owner_taps())
    res = await t.twitter_post(TwitterPostAction(text="Bought 0.05 ETH of PNL."),
                               execution_context=ctx)
    await tap
    assert res.error is None, res.error
    t.client.create_tweet.assert_called_once()


# --- a LATE approval sends the approved post once and never re-runs the job ----
# Prod risk (2026-10-08): an approval after the wait re-armed the cron job, and
# the re-run repeated the WHOLE buyback — a second swap — to redeem one post.

@pytest.mark.asyncio
async def test_late_approval_posts_once_with_the_approved_text_and_no_second_swap(
        monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    from cron.jobs import CronJob, CronJobStore
    from core.approved_actions import runner_for
    from core.wake_queue import get_wake_queue
    from tools.controller.approval_queue import decide_tool_approval
    import polyrob_x.cron_delivery as cd

    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    t = _tool(monkeypatch, require_approval=False)
    t.client.create_tweet.return_value = _resp()
    monkeypatch.setattr(cd, "_build_twitter_tool", lambda config, container: t)
    board, pushed = _real_queue(monkeypatch, tmp_path, wait_s=0.3)

    # The buyback job that swapped, then tried to post. Its next run is days away.
    store = CronJobStore(str(tmp_path / "cron.db"))
    later = datetime.now() + timedelta(days=2)
    store.add(CronJob(id="job-agent-buyback", task="PNL buyback: swap, then post", schedule_spec="4h",
                      user_id="u1", next_run_at=later, payload={"authored_by": "agent"}))
    ctx = _cron_ctx(monkeypatch, "cron-late-1", "job-agent-buyback", owner_job=False)
    text = "Bought 0.05 ETH of PNL.\n\ntx 0xabc"

    res = await t.twitter_post(TwitterPostAction(text=text), execution_context=ctx)
    assert res.error and "waiting for the owner's approval" in res.error
    t.client.create_tweet.assert_not_called()
    ask = board.asks(user_id="u1", status="open")[0]
    assert ask.payload.get("poller_gone") and not ask.payload.get("cron_job_id")

    # The owner approves AFTER the wait ended.
    ok, msg = decide_tool_approval(board, ask.id, user_id="u1", approved=True)
    assert ok and "not re-run" in msg
    # NO re-arm: the job (and its swap) does not run again for this approval.
    assert store.get("job-agent-buyback").next_run_at > datetime.now() + timedelta(days=1)

    # The agent process's wake drain sends the approved post — once.
    queue = get_wake_queue(str(tmp_path / "wakes.db"))
    rows = queue.claim_pending("test")
    assert len(rows) == 1 and rows[0].metadata["kind"] == "approved_outbound"
    runner = runner_for(rows[0].metadata)
    assert await runner(dict(rows[0].metadata), "u1", None) is True
    t.client.create_tweet.assert_called_once()
    assert t.client.create_tweet.call_args.kwargs.get("text") == text
    # A second drain (or a duplicate row) sends nothing: the grant is used.
    assert await runner(dict(rows[0].metadata), "u1", None) is True
    t.client.create_tweet.assert_called_once()
    assert any("sent (once" in p for p in pushed)


@pytest.mark.asyncio
async def test_an_approval_inside_the_wait_is_sent_by_the_run_not_the_drain(
        monkeypatch, tmp_path):
    import asyncio as _asyncio
    from core.wake_queue import get_wake_queue
    from tools.controller.approval_queue import decide_tool_approval
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    t = _tool(monkeypatch, require_approval=False)
    t.client.create_tweet.return_value = _resp()
    board, _ = _real_queue(monkeypatch, tmp_path, wait_s=5.0)
    ctx = _cron_ctx(monkeypatch, "cron-late-2", "job-x", owner_job=False)

    async def _tap():
        for _ in range(100):
            asks = board.asks(user_id="u1", status="open")
            if asks:
                decide_tool_approval(board, asks[0].id, user_id="u1", approved=True)
                return
            await _asyncio.sleep(0.02)

    tap = _asyncio.create_task(_tap())
    res = await t.twitter_post(TwitterPostAction(text="hello"), execution_context=ctx)
    await tap
    assert res.error is None
    t.client.create_tweet.assert_called_once()
    assert get_wake_queue(str(tmp_path / "wakes.db")).claim_pending("t") == []


# --- prod 2026-10-09 00:03 (buyback 4def3260d811): the controller killed the wait ---
# The in-run wait used the 300s owner_queue budget, but the controller cuts every
# `twitter` action at its tool timeout (60s default). The tool's own "waiting for
# the owner's approval" result never fired; the run saw a bare "timed out after
# 60 seconds" and retried the post. The wait must end inside the action budget.

@pytest.mark.asyncio
async def test_approval_wait_ends_inside_the_controller_action_timeout(monkeypatch, tmp_path):
    import asyncio as _asyncio
    from agents.task.constants import TimeoutConfig
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    t = _tool(monkeypatch, require_approval=False)
    board, _pushed = _real_queue(monkeypatch, tmp_path, wait_s=300.0)
    monkeypatch.setitem(TimeoutConfig.TOOL_TIMEOUTS, "default", 1)
    monkeypatch.delitem(TimeoutConfig.TOOL_TIMEOUTS, "twitter", raising=False)
    ctx = _cron_ctx(monkeypatch, "cron-prod-3", "job-4def-c", owner_job=False)
    res = await _asyncio.wait_for(
        t.twitter_post(TwitterPostAction(text="Bought 0.05 ETH of PNL."),
                       execution_context=ctx),
        timeout=TimeoutConfig.get_tool_timeout("twitter"))
    assert res.error and "waiting for the owner's approval" in res.error
    assert len(board.asks(user_id="u1", status="open")) == 1
    t.client.create_tweet.assert_not_called()
