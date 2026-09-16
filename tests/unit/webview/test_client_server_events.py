"""Client/server Socket.IO event registry stays in sync (review W7).

The legacy ``chat.js`` transcript was deleted in 043 phase 2 (§9); the new
console's transcript is ``webview/static/app/transcript.js``, which consumes the
one live event the server emits into a session room — ``feed_update``
(``webview/emit_api.py`` / ``webview/server.py::_emit_feed_event``). Pin: the new
client only listens for that event and ships no dead ``streaming_output`` /
``stream_update`` handlers (event names no server code path emits, which read as
live wiring and mask real registry drift). (``event-filter.js``'s
``streaming_output`` is a FEED KIND, not a socket event — unrelated naming
collision, deliberately untouched.)
"""
from pathlib import Path

import webview


def _app_js(name: str) -> str:
    return (Path(webview.__file__).parent / "static" / "app" / name).read_text()


def test_transcript_js_has_no_dead_stream_handlers():
    js = _app_js("transcript.js")
    assert "streaming_output" not in js
    assert "stream_update" not in js


def test_transcript_js_listens_for_the_live_feed_event():
    assert "feed_update" in _app_js("transcript.js")
