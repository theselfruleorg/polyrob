"""ops_alert self-sufficiency upgrade: doc attachment + md render + long-body spill.

Pure builders only — no network. The Telegram send funcs are exercised by
constructing the multipart body and asserting its shape.
"""
import importlib.util
import os
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "ops_alert",
    Path(__file__).resolve().parents[3] / "scripts" / "ops_alert.py",
)
ops_alert = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ops_alert)


def test_parse_args_message_and_docs():
    ns = ops_alert.parse_args(["hello world", "--doc", "a.md", "--doc", "b.png"])
    assert ns.message == "hello world"
    assert ns.docs == ["a.md", "b.png"]


def test_parse_args_render_flag():
    ns = ops_alert.parse_args(["msg", "--doc", "x.md", "--render"])
    assert ns.render is True


def test_multipart_body_has_fields_and_file():
    body, content_type = ops_alert.build_document_multipart(
        chat_id="123", caption="see attached", filename="report.html",
        file_bytes=b"<h1>hi</h1>", mime="text/html")
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"' in body and b"123" in body
    assert b'name="caption"' in body and b"see attached" in body
    assert b'name="document"; filename="report.html"' in body
    assert b"<h1>hi</h1>" in body
    assert b"text/html" in body


def test_long_message_spills_to_file():
    # A message over the Telegram text cap must be delivered as a document,
    # never silently truncated.
    long = "x" * 5000
    assert ops_alert.needs_spill(long) is True
    assert ops_alert.needs_spill("short") is False


def test_render_markdown_to_html_basic():
    md = "# Title\n\nSome **bold** and `code`.\n\n- one\n- two\n"
    html = ops_alert.render_markdown(md)
    assert "<!doctype html>" in html.lower()
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html
    assert "<li>one</li>" in html and "<li>two</li>" in html


def test_render_escapes_html_injection():
    html = ops_alert.render_markdown("watch <script>alert(1)</script> out")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_fenced_code_block_preserved():
    md = "text\n\n```\na = 1 < 2\n```\n"
    html = ops_alert.render_markdown(md)
    assert "<pre" in html and "a = 1 &lt; 2" in html


def test_owner_chat_id_prefers_explicit(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "999")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111,222")
    assert ops_alert._owner_chat_id() == "999"


def test_console_link_appended_when_env_set(monkeypatch):
    monkeypatch.setenv("OPS_CONSOLE_URL", "https://console.example")
    body = ops_alert.compose_message("did a thing")
    assert "https://console.example" in body


def test_console_link_absent_when_unset(monkeypatch):
    monkeypatch.delenv("OPS_CONSOLE_URL", raising=False)
    body = ops_alert.compose_message("did a thing")
    assert "console" not in body.lower()
