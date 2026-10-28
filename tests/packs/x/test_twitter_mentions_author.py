"""`twitter_get_mentions` must name who wrote each mention.

Prod 2026-10-27 12:01Z (X engagement cron): the read returned only id + text,
so the model guessed the author of "Please send me DM for promotions" and
replied "@<owner handle> Noted. I don't do paid promotion…" — tagging the
owner in a reply to a stranger. The API had the author all along
(`author_id` was requested and then dropped).
"""
import pytest

pytest.importorskip("polyrob_x")

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from tests.packs.x.test_twitter_actions import _tool
from polyrob_x.twitter_tool import TwitterMentionsAction


def _rows(res):
    return json.loads(res.extracted_content.split("\n", 1)[1])


@pytest.mark.asyncio
async def test_each_mention_carries_its_author(monkeypatch):
    t = _tool(monkeypatch, enabled_env=False)
    t._enabled = True
    t.get_authenticated_user_id = AsyncMock(return_value="999")
    t.client.get_users_mentions = MagicMock(return_value=SimpleNamespace(
        data=[SimpleNamespace(id=1, text="@bot please DM me", author_id=42),
              SimpleNamespace(id=2, text="@bot hi", author_id=77)],
        includes={"users": [SimpleNamespace(id=42, username="promo_acct")]}))
    res = await t.twitter_get_mentions(TwitterMentionsAction(max_results=10))
    assert res.error is None
    kwargs = t.client.get_users_mentions.call_args.kwargs
    assert "author_id" in kwargs["expansions"] and "username" in kwargs["user_fields"]
    rows = _rows(res)
    assert rows[0] == {"id": "1", "text": "@bot please DM me",
                       "author_id": "42", "author_username": "promo_acct"}
    # An author X did not expand stays unnamed — never guessed.
    assert rows[1]["author_id"] == "77" and rows[1]["author_username"] is None
