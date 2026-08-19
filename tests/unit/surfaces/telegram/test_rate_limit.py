"""TelegramRateLimiter — the RetryAfter penalty tracker TelegramSurface backs off with.

(Was in test_utils_port.py, which also pinned the markdown_v2 escaper; that escaper is
gone — core/surfaces/rendering.py is the one renderer — so the limiter coverage moved here.)
"""
import pytest

from surfaces.telegram.rate_limit import TelegramRateLimiter


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
