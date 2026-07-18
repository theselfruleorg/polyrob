"""Regression (P1): CommandContext.emit() is the choke point for slash-command
output (/memory search, /export, …). It never scrubbed secrets, unlike the
tool-trace renderer, so stored/tool content with credential shapes leaked to the
terminal. emit() must scrub.
"""
import io
import sys

from cli.ui.commands.registry import CommandContext


def _emit(text):
    ctx = CommandContext.__new__(CommandContext)
    ctx.renderer = None  # plain path → print()
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        ctx.emit(text)
    finally:
        sys.stdout = old
    return buf.getvalue()


def test_emit_scrubs_api_key():
    out = _emit("leaked OPENAI_API_KEY=sk-abcdef1234567890abcdef here")
    assert "sk-abcdef1234567890abcdef" not in out
    assert "redacted" in out.lower()


def test_emit_passes_clean_text_through():
    out = _emit("just a normal summary line")
    assert "just a normal summary line" in out
