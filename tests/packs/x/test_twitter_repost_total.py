"""Every tweet read carries ``reposts_and_quotes`` = retweet_count + quote_count.

Prod 2026-10-19..21: a growth job's goal was "100 reposts". X shows retweets and
quotes as one repost figure (the owner's app read 136), but the read handed the
model the raw ``public_metrics``, which splits them. The model read
``retweet_count`` (77) as the repost total and kept pushing a goal that crossed
100 on 10-19 (65 retweets + 70 quotes) until the owner corrected it.
"""
import pytest

pytest.importorskip("polyrob_x")

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.packs.x.test_twitter_actions import _tool
from polyrob_x.metrics import with_repost_total

_PM = {"retweet_count": 65, "quote_count": 70, "like_count": 103, "reply_count": 4}


def test_with_repost_total_adds_the_sum_and_keeps_the_raw_fields():
    out = with_repost_total(dict(_PM))
    assert out["reposts_and_quotes"] == 135
    assert out["retweet_count"] == 65 and out["quote_count"] == 70


def test_with_repost_total_is_safe_on_empty_or_odd_input():
    assert with_repost_total({}) == {"reposts_and_quotes": 0}
    assert with_repost_total(None) == {}
    assert with_repost_total({"retweet_count": "x", "quote_count": 2})["reposts_and_quotes"] == 2


@pytest.mark.asyncio
async def test_get_tweet_carries_the_repost_total(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    data = SimpleNamespace(text="hi", author_id="1", created_at=datetime(2026, 10, 21),
                           conversation_id="9", public_metrics=dict(_PM),
                           referenced_tweets=None)
    t.client.get_tweet = MagicMock(return_value=MagicMock(data=data, includes={}))
    tweet = await t.get_tweet("9")
    assert tweet["public_metrics"]["reposts_and_quotes"] == 135


@pytest.mark.asyncio
async def test_timeline_rows_carry_the_repost_total(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    rows = [SimpleNamespace(id="1", text="t", created_at=None, public_metrics=dict(_PM))] * 5
    t.client.get_users_tweets = MagicMock(return_value=MagicMock(data=rows))
    out = await t.get_user_timeline("123456", max_results=5)
    assert out[0]["metrics"]["reposts_and_quotes"] == 135
