"""113 W5 item 5 (C-05, C-07) — a chat-box verb's answer is kept in the feed,
and the feed pages back with ``skip``.

A verb typed in the chat box never reaches the agent, so nothing wrote it to the
feed: a reload lost a ``/status`` answer and its buttons. ``record_command_reply``
writes it through ``SessionManager.add_to_feed`` (the one feed writer).
"""
import asyncio
import json
import os
import time

import pytest

from webview.command_feed import record_command_reply


class _Manager:
    def __init__(self):
        self.calls = []

    def add_to_feed(self, session_id, event_type, data, agent_id=None):
        self.calls.append((session_id, event_type, data))


class _Agent:
    def __init__(self, manager):
        self.session_manager = manager


def test_an_answer_is_written_with_its_line():
    m = _Manager()
    out = asyncio.run(record_command_reply(_Agent(m), "sess1", "/status", "All good."))
    assert out == "All good."
    assert m.calls == [("sess1", "command_reply",
                        {"text": "All good.", "line": "/status", "inline": True})]


@pytest.mark.parametrize("clean_id,reply", [("", "x"), ("sess1", None), ("sess1", "  ")])
def test_nothing_to_keep_writes_nothing(clean_id, reply):
    m = _Manager()
    assert asyncio.run(record_command_reply(_Agent(m), clean_id, "/x", reply)) == reply
    assert m.calls == []


def test_a_console_seat_is_not_a_chat(monkeypatch):
    import agents.task.path as pth
    monkeypatch.setattr(pth, "is_reserved_session_id", lambda sid: True)
    m = _Manager()
    asyncio.run(record_command_reply(_Agent(m), "money", "/book", "Book."))
    assert m.calls == []


def test_a_failed_write_never_loses_the_answer():
    class _Broken:
        def add_to_feed(self, *a, **k):
            raise OSError("disk full")
    assert asyncio.run(record_command_reply(_Agent(_Broken()), "s", "/status", "ok")) == "ok"
    assert asyncio.run(record_command_reply(None, "s", "/status", "ok")) == "ok"


def test_the_route_delegator_keeps_the_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    import webview.server as srv
    import webview.console_commands as cc
    m = _Manager()

    async def _answer(agent, clean_id, user_id, text):
        return "All good."
    monkeypatch.setattr(cc, "maybe_handle_console_command", _answer)
    monkeypatch.setattr(srv, "_in_process_task_agent", lambda: _Agent(m))
    assert asyncio.run(srv._maybe_handle_console_command("sess1", "alice", "/status")) == "All good."
    assert m.calls and m.calls[0][1] == "command_reply"


# --- "load older": the feed pages back --------------------------------------- #

def test_skip_pages_back_through_the_feed(tmp_path):
    from webview.feed_routes import read_feed
    base = time.time() - 1000
    for i in range(10):
        p = tmp_path / f"agent_message_{int((base + i) * 1000)}.json"
        p.write_text(json.dumps({"type": "agent_message", "timestamp": base + i, "data": {"n": i}}))
        os.utime(p, (base + i, base + i))
    first = read_feed(tmp_path, event_type=None, limit=4, after_seq=None)
    assert [e["data"]["n"] for e in first["events"]] == [6, 7, 8, 9]
    assert first["has_more"] is True and first["read"] == 4
    second = read_feed(tmp_path, event_type=None, limit=4, after_seq=None, skip=first["read"])
    assert [e["data"]["n"] for e in second["events"]] == [2, 3, 4, 5]
    last = read_feed(tmp_path, event_type=None, limit=4, after_seq=None, skip=8)
    assert [e["data"]["n"] for e in last["events"]] == [0, 1] and last["has_more"] is False


def test_command_reply_is_a_known_type():
    from webview.feed_routes import VALID_EVENT_TYPES
    assert "command_reply" in VALID_EVENT_TYPES


def test_the_thread_is_a_log_region():
    from pathlib import Path
    import webview
    chat = (Path(webview.__file__).parent / "templates" / "chat.html").read_text()
    assert 'id="chat-messages" role="log" aria-live="polite"' in chat
