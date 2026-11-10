"""113 W6 item 3 (W2 item 6, IA-1): the chat column on the Chat destination.

At 1024 px and wider the Chat destination shows the caller's console chats in a
left column (main first, then the separate chats by last activity; the open one
is ``aria-current``), fed by ``GET /api/webgate/chats`` — the rows that carry
``chat``. Below 1024 px nothing changes: the Chats overlay stays the way in.
"""
import os
import re
import time
import types
from pathlib import Path

import pytest

WEBVIEW = Path(__file__).resolve().parents[3] / "webview"


def test_the_chat_row_says_when_it_last_changed(monkeypatch, tmp_path):
    import webview.chat_open as chat_open
    import webview.chats_routes as mod
    folder = tmp_path / "s1"
    (folder / "feed").mkdir(parents=True)
    then = time.time() - 3600
    os.utime(folder, (then, then))
    monkeypatch.setattr(chat_open, "_session_folder", lambda sid: folder if sid == "s1" else None)
    when = mod._last_active("s1")
    assert when and when.endswith("+00:00")
    assert mod._last_active("nope") is None


def test_a_failed_read_is_not_known(monkeypatch):
    import webview.chat_open as chat_open
    import webview.chats_routes as mod

    def boom(sid):
        raise OSError("disk")
    monkeypatch.setattr(chat_open, "_session_folder", boom)
    assert mod._last_active("s1") is None


def test_the_column_is_on_the_chat_destination_only_and_has_no_inline_script():
    chat = (WEBVIEW / "templates" / "chat.html").read_text()
    assert 'id="chat-column"' in chat
    assert '<script src="/static/app/chat-column.js" type="module"></script>' in chat
    assert "<nav" not in chat.split('id="chat-column"', 1)[1].split("</aside>", 1)[0]
    shell = (WEBVIEW / "templates" / "shell.html").read_text()
    assert "chat-column" not in shell


def test_below_1024_the_column_is_not_drawn():
    css = (WEBVIEW / "static" / "app" / "app.css").read_text()
    assert re.search(r"\.chat-column\s*\{[^}]*display:\s*none", css)
    assert "@media (min-width:1024px)" in css
    js = (WEBVIEW / "static" / "app" / "chat-column.js").read_text()
    assert "(min-width: 1024px)" in js        # no read below 1024 px
