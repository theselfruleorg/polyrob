"""P4: lock the ported pure utils we depend on (markdown_v2 escaping + rate limiter).

These were ported as-is from the old bot; this pins the behaviors TelegramSurface
relies on so a future edit can't silently regress them.
"""
import pytest

from surfaces.telegram import markdown as md
from surfaces.telegram.rate_limit import TelegramRateLimiter


def test_escape_markdown_v2_escapes_special_chars():
    out = md.escape_markdown_v2("a.b-c!(x)")
    # MarkdownV2 reserves . - ! ( ) — each must be backslash-escaped.
    for ch in (".", "-", "!", "(", ")"):
        assert f"\\{ch}" in out


def test_escape_markdown_v2_empty_is_safe():
    assert md.escape_markdown_v2("") == ""


def test_safe_markdown_message_returns_text_and_parsemode():
    text, parse_mode = md.safe_markdown_message("hello world")
    assert isinstance(text, str)
    # parse_mode is either a mode string or None; must not raise.


@pytest.mark.asyncio
async def test_rate_limiter_records_and_clears_penalty():
    rl = TelegramRateLimiter()
    chat = 555
    # No penalty initially.
    penalized, remaining = await rl.is_chat_penalized(chat)
    assert penalized is False
    # Record a RetryAfter penalty.
    await rl.record_penalty(chat, retry_after=5.0, operation="edit")
    penalized, remaining = await rl.is_chat_penalized(chat)
    assert penalized is True
    assert remaining > 0
    # Reset clears it.
    await rl.reset_chat(chat)
    penalized, _ = await rl.is_chat_penalized(chat)
    assert penalized is False
