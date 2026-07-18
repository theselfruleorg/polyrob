"""X DM poller — since-id cursor semantics + rate-limit backoff (no network)."""
import asyncio
import time

import pytest

from surfaces.x.client import XRateLimited
from surfaces.x.poller import XCursorStore, XDMPoller


class FakeClient:
    """Scripted pages: list of {"events": [...], "next_token": ...} per call.

    Newest-first within a page, like the real GET /2/dm_events.
    """

    def __init__(self, pages=None, error=None):
        self.pages = list(pages or [])
        self.error = error
        self.calls = []

    async def get_dm_events(self, pagination_token=None):
        self.calls.append(pagination_token)
        if self.error is not None:
            raise self.error
        if not self.pages:
            return {"events": [], "next_token": None}
        return self.pages.pop(0)


def _ev(eid, text="hi", sender="42"):
    return {"id": str(eid), "event_type": "MessageCreate", "text": text,
            "sender_id": sender, "dm_conversation_id": f"{sender}-999"}


def _poller(client, cursor, handled, **kw):
    async def handler(event):
        handled.append(event["id"])
    return XDMPoller(client, handler, cursor, **kw)


def test_cursor_store_roundtrip(tmp_path):
    store = XCursorStore(str(tmp_path / "x_cursor.json"))
    assert store.get() is None
    store.set("123")
    assert store.get() == "123"
    # survives a fresh open (persisted)
    assert XCursorStore(str(tmp_path / "x_cursor.json")).get() == "123"


def test_first_run_initializes_cursor_without_replaying(tmp_path):
    cursor = XCursorStore(str(tmp_path / "c.json"))
    client = FakeClient(pages=[{"events": [_ev(120), _ev(110)], "next_token": None}])
    handled = []
    n = asyncio.run(_poller(client, cursor, handled).poll_once())
    assert n == 0
    assert handled == []
    assert cursor.get() == "120"


def test_new_events_handled_oldest_first_and_cursor_advances(tmp_path):
    cursor = XCursorStore(str(tmp_path / "c.json"))
    cursor.set("100")
    client = FakeClient(pages=[
        {"events": [_ev(130), _ev(120), _ev(100)], "next_token": None}])
    handled = []
    n = asyncio.run(_poller(client, cursor, handled).poll_once())
    assert n == 2
    assert handled == ["120", "130"]
    assert cursor.get() == "130"


def test_pagination_stops_at_cursor(tmp_path):
    cursor = XCursorStore(str(tmp_path / "c.json"))
    cursor.set("100")
    client = FakeClient(pages=[
        {"events": [_ev(140), _ev(130)], "next_token": "tok2"},
        {"events": [_ev(120), _ev(100)], "next_token": "tok3"},
    ])
    handled = []
    n = asyncio.run(_poller(client, cursor, handled).poll_once())
    assert n == 3
    assert handled == ["120", "130", "140"]
    assert cursor.get() == "140"
    # cursor hit on page 2 → page 3 never fetched
    assert client.calls == [None, "tok2"]


def test_page_cap_bounds_rate_limit_burn(tmp_path):
    cursor = XCursorStore(str(tmp_path / "c.json"))
    cursor.set("1")
    pages = [{"events": [_ev(100 - i)], "next_token": f"t{i}"} for i in range(10)]
    client = FakeClient(pages=pages)
    handled = []
    asyncio.run(_poller(client, cursor, handled, max_pages=3).poll_once())
    assert len(client.calls) == 3


def test_cursor_advances_past_unhandled_own_events(tmp_path):
    # The poller advances the cursor over EVERY new event id (the handler decides
    # what to skip) so an own outbound echo is never re-fetched forever.
    cursor = XCursorStore(str(tmp_path / "c.json"))
    cursor.set("100")
    client = FakeClient(pages=[
        {"events": [_ev(130, sender="999")], "next_token": None}])
    handled = []
    asyncio.run(_poller(client, cursor, handled).poll_once())
    assert handled == ["130"]  # handed over; parse/dedup filters own messages
    assert cursor.get() == "130"


def test_rate_limited_sets_backoff_until_reset(tmp_path):
    cursor = XCursorStore(str(tmp_path / "c.json"))
    cursor.set("100")
    reset_at = time.time() + 500
    client = FakeClient(error=XRateLimited(reset_at=reset_at))
    handled = []
    poller = _poller(client, cursor, handled, poll_sec=90.0)
    n = asyncio.run(poller.poll_once())
    assert n == 0
    assert handled == []
    delay = poller.next_delay()
    assert 400 < delay <= 510  # waits for the reset, not a fixed sleep


def test_normal_delay_is_poll_sec(tmp_path):
    cursor = XCursorStore(str(tmp_path / "c.json"))
    client = FakeClient(pages=[{"events": [], "next_token": None}])
    poller = _poller(client, cursor, [], poll_sec=77.0)
    asyncio.run(poller.poll_once())
    assert poller.next_delay() == pytest.approx(77.0)


def test_transient_error_does_not_crash_or_move_cursor(tmp_path):
    cursor = XCursorStore(str(tmp_path / "c.json"))
    cursor.set("100")
    client = FakeClient(error=RuntimeError("boom"))
    handled = []
    n = asyncio.run(_poller(client, cursor, handled).poll_once())
    assert n == 0
    assert cursor.get() == "100"
