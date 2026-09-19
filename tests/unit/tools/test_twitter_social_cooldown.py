"""2026-08-28 — durable, cross-session cooldown for autonomous twitter posts.

Observed in production: a recurring "track record" goal re-fired
repeatedly — each run a FRESH session with its own fresh TwitterTool
instance — and posted 30+ near-duplicate tweets over ~5 days. The existing
`_rate_available` rate limiter is an in-memory per-instance bucket, so it
never saw a sibling session's posts. This durable, event-log-backed check
is scoped to autonomous/forged turns only (twitter_post/twitter_thread) —
a genuine owner-driven post, a reply/quote, or execution_context=None
(owner-direct/CLI) are never gated.
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


def _tool(monkeypatch):
    monkeypatch.setenv("TWITTER_ENABLED", "true")
    monkeypatch.setenv("TWITTER_REQUIRE_APPROVAL", "false")
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw-cooldown-test")
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


def _resp(tid="111", text="hi"):
    return MagicMock(data={"id": tid, "text": text})


def _autonomous_ctx(session_id="s1", user_id="u1"):
    # role defaults to "leaf" — the same shape a goal/cron-spawned turn has.
    return ActionExecutionContext(session_id=session_id, user_id=user_id)


def _owner_ctx(session_id="s1", user_id="u1"):
    return ActionExecutionContext(session_id=session_id, user_id=user_id, role="orchestrator")


@pytest.mark.asyncio
async def test_repeat_autonomous_post_blocked_within_cooldown(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.return_value = _resp("1")
    ctx = _autonomous_ctx()
    res1 = await t.twitter_post(TwitterPostAction(text="first"), execution_context=ctx)
    assert res1.error is None
    res2 = await t.twitter_post(
        TwitterPostAction(text="second, different wording entirely"), execution_context=ctx)
    assert res2.error is not None
    assert "cooldown" in res2.error.lower()
    # only the first call reached the client
    assert t.client.create_tweet.call_count == 1


@pytest.mark.asyncio
async def test_owner_interactive_post_never_gated(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("1"), _resp("2")]
    ctx = _owner_ctx()
    res1 = await t.twitter_post(TwitterPostAction(text="first"), execution_context=ctx)
    assert res1.error is None
    res2 = await t.twitter_post(TwitterPostAction(text="second"), execution_context=ctx)
    assert res2.error is None


@pytest.mark.asyncio
async def test_no_execution_context_never_gated(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("1"), _resp("2")]
    res1 = await t.twitter_post(TwitterPostAction(text="first"))
    assert res1.error is None
    res2 = await t.twitter_post(TwitterPostAction(text="second"))
    assert res2.error is None


@pytest.mark.asyncio
async def test_reply_and_quote_are_never_gated_by_the_post_cooldown(monkeypatch):
    """A reply to a DIFFERENT tweet is not a repeat by construction."""
    from tools.twitter_tool import TwitterQuoteAction
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("1"), _resp("2"), _resp("3")]
    ctx = _autonomous_ctx()
    res1 = await t.twitter_post(TwitterPostAction(text="first"), execution_context=ctx)
    assert res1.error is None
    res2 = await t.twitter_reply(TwitterReplyAction(tweet_id="99", text="reply"), execution_context=ctx)
    assert res2.error is None
    res3 = await t.twitter_quote(TwitterQuoteAction(tweet_id="99", text="quote"), execution_context=ctx)
    assert res3.error is None


@pytest.mark.asyncio
async def test_thread_is_gated_by_a_prior_post_cooldown(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.return_value = _resp("1")
    ctx = _autonomous_ctx()
    res1 = await t.twitter_post(TwitterPostAction(text="first"), execution_context=ctx)
    assert res1.error is None
    res2 = await t.twitter_thread(TwitterThreadAction(texts=["a", "b"]), execution_context=ctx)
    assert res2.error is not None
    assert "cooldown" in res2.error.lower()


@pytest.mark.asyncio
async def test_disabled_flag_restores_legacy_behavior(monkeypatch):
    monkeypatch.setenv("TWITTER_POST_COOLDOWN_ENABLED", "false")
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("1"), _resp("2")]
    ctx = _autonomous_ctx()
    res1 = await t.twitter_post(TwitterPostAction(text="first"), execution_context=ctx)
    assert res1.error is None
    res2 = await t.twitter_post(TwitterPostAction(text="second"), execution_context=ctx)
    assert res2.error is None


@pytest.mark.asyncio
async def test_different_tenant_not_gated_by_anothers_cooldown(monkeypatch):
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("1"), _resp("2")]
    res1 = await t.twitter_post(
        TwitterPostAction(text="first"), execution_context=_autonomous_ctx(user_id="userA"))
    assert res1.error is None
    res2 = await t.twitter_post(
        TwitterPostAction(text="second"),
        execution_context=_autonomous_ctx(session_id="s2", user_id="userB"))
    assert res2.error is None


@pytest.mark.asyncio
async def test_cooldown_expires_after_the_configured_window(monkeypatch):
    monkeypatch.setenv("TWITTER_POST_COOLDOWN_SEC", "0")
    t = _tool(monkeypatch)
    t.client.create_tweet.side_effect = [_resp("1"), _resp("2")]
    ctx = _autonomous_ctx()
    res1 = await t.twitter_post(TwitterPostAction(text="first"), execution_context=ctx)
    assert res1.error is None
    res2 = await t.twitter_post(TwitterPostAction(text="second"), execution_context=ctx)
    assert res2.error is None


@pytest.mark.asyncio
async def test_cooldown_refusal_teaches_the_reschedule_not_the_drop(monkeypatch):
    """2026-09-17 (intel 2169): buyback tranche 3 broadcast two seconds after the
    status-post cron consumed the cooldown; the refusal said "likely a re-fire"
    and the agent dropped a report the owner had asked for on EVERY tranche.
    The refusal must carry the remaining seconds and the exact way to defer the
    post (a one-shot cron after the window) so a NEW-fact post is deferred, not
    dropped — while a true re-fire is still told not to repeat itself."""
    t = _tool(monkeypatch)
    t.client.create_tweet.return_value = _resp("1")
    ctx = _autonomous_ctx()
    await t.twitter_post(TwitterPostAction(text="tranche 2 tx 0xaaa"), execution_context=ctx)
    res = await t.twitter_post(TwitterPostAction(text="tranche 3 tx 0xbbb"), execution_context=ctx)
    err = res.error or ""
    assert "retry_after_sec=" in err
    assert "cronjob_schedule" in err
    assert "one-shot ISO" in err and "schedule_spec='20" in err  # an ISO instant, not a duration
    assert "do not drop" in err.lower()
    assert "re-fire" in err.lower()
