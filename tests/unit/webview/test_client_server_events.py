"""Client/server Socket.IO event registry stays in sync (review W7).

The server emits exactly ``stream_chunk`` for chat streaming; ``chat.js``
shipped dead handlers for ``streaming_output``/``stream_update`` — event
names no server code path emits — which reads as live wiring and masks real
registry drift. Pin: the client only listens for events the server emits.
(``event-filter.js``'s ``streaming_output`` is a FEED KIND, not a socket
event — unrelated naming collision, deliberately untouched.)
"""
from pathlib import Path

import webview


def _js(name: str) -> str:
    return (Path(webview.__file__).parent / "static" / "js" / name).read_text()


def test_chat_js_has_no_dead_stream_handlers():
    js = _js("chat.js")
    assert "streaming_output" not in js
    assert "stream_update" not in js


def test_chat_js_still_handles_the_real_stream_event():
    assert "stream_chunk" in _js("chat.js")
