"""2026-10-14 review C-11 — the DM conversation list carries last_event_at.

Run 3609ed72 read 19 X Chat threads one by one (7 calls, 0.51M tokens) for
"nothing new": the list had no last-message time. Each conversation now has
`last_event_at`, the list is newest first with a `watermark`, and `since`
returns only threads newer than the last sweep. A thread with no time is kept.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("polyrob_x")
from polyrob_x.dm_watermark import dm_list_since
from polyrob_x.twitter_tool import TwitterGetDMsAction

CONVS = [{"id": "a", "updated_at": "2026-10-14T08:00:00Z"},
         {"id": "b", "created_at": "2026-10-14T10:00:00Z"},
         {"id": "c"},
         {"id": "d", "updated_at": "2026-10-13T08:00:00.000Z"}]


def test_sorted_newest_first_with_watermark():
    out, mark, dropped = dm_list_since(CONVS, None)
    assert [c["id"] for c in out][:3] == ["c", "b", "a"]   # unknown time sorts first
    assert out[-1]["id"] == "d" and out[-1]["last_event_at"].startswith("2026-10-13")
    assert mark == "2026-10-14T10:00:00Z" and dropped == 0


def test_since_drops_only_known_older_threads():
    out, mark, dropped = dm_list_since(CONVS, "2026-10-14T09:00:00Z")
    assert {c["id"] for c in out} == {"b", "c"} and dropped == 2
    assert mark == "2026-10-14T10:00:00Z"
    out, mark, _ = dm_list_since([], "2026-10-14T09:00:00Z")
    assert out == [] and mark == "2026-10-14T09:00:00Z"   # the watermark is kept


@pytest.mark.asyncio
async def test_tool_list_read_returns_watermark(monkeypatch):
    from tests.packs.x.test_twitter_dm_oauth1_fallback import _tool
    t = _tool(monkeypatch)
    t.chat_client = MagicMock()
    t.chat_client.get_conversations = AsyncMock(return_value={"data": CONVS, "meta": {}})
    monkeypatch.setattr(type(t), "_oauth2_relogin_needed", lambda self: False, raising=False)
    res = await t.twitter_get_dms(TwitterGetDMsAction(since="2026-10-14T09:00:00Z"))
    assert res.error is None, res.error
    payload = json.loads(res.extracted_content.split("\n", 1)[1])
    assert payload["watermark"] == "2026-10-14T10:00:00Z"
    assert payload["older_than_since"] == 2
    assert all("last_event_at" in c for c in payload["conversations"])
