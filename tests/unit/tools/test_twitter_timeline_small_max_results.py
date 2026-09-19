"""`twitter_get_timeline(max_results<5)` must work, not be rejected at the schema.

Prod evidence (2026-09-18, 24 h): 8 paid steps died on
`max_results=3 … greater_than_equal 5` — the model asks for "the last 3 posts"
and the schema leaked the X API's floor (5..100) as a hard validation error.
The floor is the API's, not the user's: accept 1..100, request max(5, n) from
the API and return only n rows.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.unit.tools.test_twitter_actions import _tool
from tools.twitter_tool import TwitterTimelineAction


def _tweets(n):
    return [SimpleNamespace(id=str(i), text=f"t{i}", created_at=None, public_metrics={}) for i in range(n)]


def test_schema_accepts_small_counts():
    assert TwitterTimelineAction(user="x", max_results=3).max_results == 3
    assert TwitterTimelineAction(user="x", max_results=1).max_results == 1
    with pytest.raises(Exception):
        TwitterTimelineAction(user="x", max_results=0)
    with pytest.raises(Exception):
        TwitterTimelineAction(user="x", max_results=101)


@pytest.mark.asyncio
async def test_small_count_is_clamped_for_the_api_and_truncated_for_the_caller(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    t.client.get_users_tweets = MagicMock(return_value=MagicMock(data=_tweets(5)))
    res = await t.twitter_get_timeline(TwitterTimelineAction(user="123456", max_results=3))
    assert res.error is None
    assert t.client.get_users_tweets.call_args.kwargs["max_results"] == 5  # API floor
    assert "Timeline for 123456 (3)" in res.extracted_content
    assert '"t4"' not in res.extracted_content and '"t2"' in res.extracted_content


@pytest.mark.asyncio
async def test_counts_at_or_above_the_floor_are_passed_through(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    t.client.get_users_tweets = MagicMock(return_value=MagicMock(data=_tweets(7)))
    res = await t.twitter_get_timeline(TwitterTimelineAction(user="123456", max_results=7))
    assert t.client.get_users_tweets.call_args.kwargs["max_results"] == 7
    assert "Timeline for 123456 (7)" in res.extracted_content
