"""A post is verified BY ID, lag-tolerant — never by a timeline scan (2026-09-26 P1-8).

The agent posted, scanned its own timeline 4 s later, missed the post (X's
timeline lags) and alarmed the owner about a post that was live. The write verbs
now read the new tweet back by id with a short retry, and a not-yet-readable id
is reported as lag, never as failure.
"""
import pytest

pytest.importorskip("polyrob_x")

import logging
from unittest.mock import AsyncMock, MagicMock

from tools.base_tool import ToolStatus
from polyrob_x.twitter_tool import TwitterPostAction, TwitterThreadAction, TwitterTool


def _tool(monkeypatch):
    monkeypatch.setenv("TWITTER_ENABLED", "true")
    monkeypatch.setenv("TWITTER_REQUIRE_APPROVAL", "false")
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw-readback")
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
    t.READBACK_DELAYS_SEC = (0.0, 0.0, 0.0)
    return t


@pytest.mark.asyncio
async def test_post_is_read_back_by_id(monkeypatch):
    t = _tool(monkeypatch)
    monkeypatch.setattr(t, "_compose_tweet", AsyncMock(return_value={"id": "2103788294454583796"}))
    got = AsyncMock(return_value={"id": "2103788294454583796", "text": "x"})
    monkeypatch.setattr(t, "get_tweet", got)
    t.client.get_users_tweets = MagicMock(side_effect=AssertionError("no timeline scan"))
    res = await t.twitter_post(TwitterPostAction(text="hello"))
    assert res.error is None
    assert "verified" in res.extracted_content and "2103788294454583796" in res.extracted_content
    got.assert_awaited_with("2103788294454583796")


@pytest.mark.asyncio
async def test_lagging_id_is_not_reported_as_failure(monkeypatch):
    t = _tool(monkeypatch)
    monkeypatch.setattr(t, "_compose_tweet", AsyncMock(return_value={"id": "55"}))
    got = AsyncMock(side_effect=[None, RuntimeError("404"), None])
    monkeypatch.setattr(t, "get_tweet", got)
    res = await t.twitter_post(TwitterPostAction(text="hello"))
    assert res.error is None                                  # the write succeeded
    assert got.await_count == 3                               # lag-tolerant retries
    text = res.extracted_content
    assert "not yet readable by id" in text and "IS the proof" in text
    assert "never by scanning" in text


@pytest.mark.asyncio
async def test_thread_reads_back_its_last_tweet(monkeypatch):
    t = _tool(monkeypatch)
    ids = iter([{"id": "1"}, {"id": "2"}])
    monkeypatch.setattr(t, "_compose_tweet", AsyncMock(side_effect=lambda **k: next(ids)))
    got = AsyncMock(return_value={"id": "2"})
    monkeypatch.setattr(t, "get_tweet", got)
    res = await t.twitter_thread(TwitterThreadAction(texts=["a", "b"]))
    assert res.error is None
    got.assert_awaited_with("2")
