"""The stale-context reaper measures IDLE time, not age.

Prod evidence (2026-09-18 07:21:57Z): `Cleaning up stale context for session
executor_fa8f19a7… (allocated 310s ago)` closed the context of a LIVE goal
session mid-run (its steps had been using it every ~40 s); the session's next
`get_context` hit `BrowserContext.new_page: Target crashed` (chrome segfault in
dmesg at 07:22:28), the step then hung to its 600 s timeout and the goal run
failed. 32 such reaps in 24 h. `BROWSER_STALE_TIMEOUT` is documented as an
*idle* timeout — every `get_context` that hands back the session's existing
context must refresh the clock so a busy session is never reaped.
"""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import BotConfig
from tools.browser.browser_manager import BrowserManager

pytestmark = pytest.mark.asyncio


def _manager():
    cfg = BotConfig()
    cfg.browser_stale_timeout = 60.0
    m = BrowserManager(config=cfg)
    m._ensure_background_tasks_started = AsyncMock()  # no reaper loop in the test
    return m


async def test_returning_the_existing_context_refreshes_the_idle_clock():
    m = _manager()
    ctx = MagicMock()
    m.contexts_in_use["executor_s1"] = ctx
    m.context_allocation_times["executor_s1"] = time.time() - 500  # old allocation
    assert await m.get_context("executor_s1") is ctx
    assert time.time() - m.context_allocation_times["executor_s1"] < 5


async def test_reaper_skips_a_context_touched_within_the_timeout():
    m = _manager()
    ctx = MagicMock()
    m.contexts_in_use["executor_s1"] = ctx
    m.context_allocation_times["executor_s1"] = time.time() - 500
    m._release_context_internal = AsyncMock()
    await m.get_context("executor_s1")  # the session is busy → touched
    await m._reap_stale_contexts_once()
    m._release_context_internal.assert_not_awaited()


async def test_reaper_still_closes_a_genuinely_idle_context():
    m = _manager()
    m.contexts_in_use["executor_s2"] = MagicMock()
    m.context_allocation_times["executor_s2"] = time.time() - 500
    m._release_context_internal = AsyncMock()
    await m._reap_stale_contexts_once()
    m._release_context_internal.assert_awaited_once_with("executor_s2", close=True)
