"""W1.3 — the console feed watcher marks a file seen only after it parsed.

It used to add the name to ``processed_files`` BEFORE ``json.load``. A
half-written ``agent_message_*.json`` failed the parse, was marked seen, and
the later ``modified`` event was skipped — Rob's reply appeared only after a
reload. A parse failure now leaves the name unmarked so the next change event
retries it.
"""
import asyncio

import pytest
from watchfiles import Change

import webview.server as server


class _PM:
    def __init__(self, feed_dir):
        self._feed_dir = feed_dir

    def clean_session_id(self, sid):
        return sid

    def get_feed_dir(self, sid):
        return self._feed_dir

    def find_feed_dir(self, sid, user_id=None):
        return self._feed_dir if self._feed_dir.is_dir() else None


@pytest.mark.asyncio
async def test_bad_parse_is_retried(tmp_path, monkeypatch):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    target = feed_dir / "agent_message_1700000000000.json"

    async def _fake_awatch(path, watch_filter=None):
        target.write_text('{"timestamp": 1, "type": "agent_mess')  # truncated
        yield {(Change.added, str(target))}
        target.write_text('{"timestamp": 1, "type": "agent_message", "data": {"text": "hi"}}')
        yield {(Change.modified, str(target))}

    emitted = []

    async def _fake_emit(entry, room):
        emitted.append((entry, room))

    monkeypatch.setattr(server, "pm", lambda: _PM(feed_dir))
    monkeypatch.setattr(server, "awatch", _fake_awatch)
    monkeypatch.setattr(server, "_emit_feed_event", _fake_emit)

    await asyncio.wait_for(server._feed_watcher("sess-retry"), timeout=5)

    assert len(emitted) == 1, emitted
    assert emitted[0][0]["type"] == "agent_message"
    assert emitted[0][1] == "sess-retry"
