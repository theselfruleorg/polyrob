"""098 CL-12: the X DM cursor means "every id at or below was handled"."""
import pytest

pytest.importorskip("polyrob_x")

import asyncio
import json

from polyrob_x.surface.poller import XCursorStore, XDMPoller


class Pages:
    """newest-first pages keyed by token (None = first page)."""

    def __init__(self, book):
        self.book = book
        self.calls = []

    async def get_dm_events(self, pagination_token=None):
        self.calls.append(pagination_token)
        return self.book.get(pagination_token, {"events": [], "next_token": None})


def _ev(eid):
    return {"id": str(eid), "event_type": "MessageCreate", "text": "hi",
            "sender_id": "42", "dm_conversation_id": "42-999"}


def _page(ids, nxt=None):
    return {"events": [_ev(i) for i in ids], "next_token": nxt}


def _poller(client, cursor, handled, fail=None, **kw):
    async def handler(ev):
        if fail is not None and fail(ev["id"]):
            raise RuntimeError("db locked")
        handled.append(ev["id"])
    return XDMPoller(client, handler, cursor, **kw)


def test_handler_failure_holds_cursor(tmp_path):
    cur = XCursorStore(str(tmp_path / "c.json"))
    cur.set("100")
    client = Pages({None: _page([106, 105, 104, 100])})
    handled = []
    asyncio.run(_poller(client, cur, handled, fail=lambda i: i == "105").poll_once())
    assert handled == ["104"]
    assert cur.get() == "104"           # below 105: it is retried
    handled2 = []
    asyncio.run(_poller(client, cur, handled2).poll_once())
    assert handled2 == ["105", "106"]
    assert cur.get() == "106"


def test_poison_event_skipped_after_five(tmp_path, monkeypatch):
    events = []
    import core.event_log
    monkeypatch.setattr(core.event_log, "emit", lambda kind, **kw: events.append(kind))
    cur = XCursorStore(str(tmp_path / "c.json"))
    cur.set("100")
    client = Pages({None: _page([102, 101, 100])})
    handled = []
    for _ in range(4):
        asyncio.run(_poller(client, cur, handled, fail=lambda i: i == "101").poll_once())
        assert cur.get() == "100"
    asyncio.run(_poller(client, cur, handled, fail=lambda i: i == "101").poll_once())
    assert cur.get() == "102" and "x_dm_poison" in events
    assert handled.count("102") == 1


def test_corrupt_cursor_uses_backup(tmp_path):
    path = tmp_path / "c.json"
    cur = XCursorStore(str(path))
    cur.set("200")
    path.write_text("{garbage")
    assert cur.get() == "200"
    client = Pages({None: _page([201, 200])})
    handled = []
    asyncio.run(_poller(client, cur, handled).poll_once())
    assert handled == ["201"]           # not the first-run branch


def test_corrupt_cursor_without_backup_tells_the_owner(tmp_path, monkeypatch):
    events = []
    import core.event_log
    monkeypatch.setattr(core.event_log, "emit", lambda kind, **kw: events.append(kind))
    path = tmp_path / "c.json"
    path.write_text("{garbage")
    assert XCursorStore(str(path)).get() is None
    assert events == ["x_dm_cursor_lost"]


def test_page_cap_drains_older_pages_next_poll(tmp_path):
    cur = XCursorStore(str(tmp_path / "c.json"))
    cur.set("100")
    client = Pages({None: _page([110, 109], "t2"), "t2": _page([108, 107], "t3"),
                    "t3": _page([106, 100])})
    handled = []
    p = _poller(client, cur, handled, max_pages=2)
    asyncio.run(p.poll_once())
    assert handled == ["107", "108", "109", "110"]
    assert cur.get() == "100"                       # not over the unfetched page
    state = cur.load()
    assert state["drain"] == {"token": "t3", "upper": "110"}
    asyncio.run(p.poll_once())
    assert handled[-1] == "106" and cur.get() == "110"
    assert cur.load()["drain"] is None


def test_v1_cursor_file_reads(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"since_id": "77"}))
    state = XCursorStore(str(path)).load()
    assert state["since_id"] == "77" and state["drain"] is None


def test_non_numeric_id_does_not_raise(tmp_path):
    cur = XCursorStore(str(tmp_path / "c.json"))
    cur.set("100")
    client = Pages({None: {"events": [_ev(101), {"id": "x", "event_type": "MessageCreate"},
                                      _ev(100)], "next_token": None}})
    handled = []
    asyncio.run(_poller(client, cur, handled).poll_once())
    assert handled == ["101"]
