"""098 TS-05: the browser context pool — one creation per session, the cap holds
under concurrency, a cancelled waiter leaves the queue, cleanup is restartable."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import BotConfig
from tools.browser.browser_manager import BrowserManager

pytestmark = pytest.mark.asyncio


def _cfg(max_contexts=2, per_session=5):
    c = BotConfig()
    c.max_browser_contexts = max_contexts
    c.max_contexts_per_session = per_session
    c.browser_context_timeout = 5.0
    c.browser_stale_timeout = 600.0
    return c


def _ctx():
    ctx = MagicMock()
    ctx.close = AsyncMock()
    ctx.reset_context = AsyncMock()
    return ctx


@patch('tools.browser.browser_manager.Browser')
async def test_concurrent_get_context_same_session_creates_once(mock_browser_class):
    gate = asyncio.Event()
    made = []

    async def new_context(config=None, session_id=None):
        await gate.wait()
        made.append(_ctx())
        return made[-1]
    mock_browser_class.return_value.new_context = new_context
    m = BrowserManager(config=_cfg())
    await m.initialize()
    a = asyncio.create_task(m.get_context("s1_agent"))
    b = asyncio.create_task(m.get_context("s1_agent"))
    await asyncio.sleep(0.01)
    gate.set()
    ca, cb = await asyncio.gather(a, b)
    assert len(made) == 1 and ca is cb is made[0]
    assert m.session_contexts["agent"] == ["s1_agent"]


@patch('tools.browser.browser_manager.Browser')
async def test_pool_respects_max_contexts_under_concurrency(mock_browser_class):
    gate = asyncio.Event()

    async def new_context(config=None, session_id=None):
        await gate.wait()
        return _ctx()
    mock_browser_class.return_value.new_context = new_context
    m = BrowserManager(config=_cfg(max_contexts=2))
    await m.initialize()
    tasks = [asyncio.create_task(m.get_context(f"s{i}")) for i in range(4)]
    await asyncio.sleep(0.01)
    gate.set()
    got = await asyncio.gather(*tasks)
    assert sum(1 for c in got if c is not None) == 2
    assert len(m.contexts_in_use) == 2 and not m._creating


@patch('tools.browser.browser_manager.Browser')
async def test_cancelled_waiter_is_removed_and_context_returned_to_pool(mock_browser_class):
    made = []

    async def new_context(config=None, session_id=None):
        made.append(_ctx())
        return made[-1]
    mock_browser_class.return_value.new_context = new_context
    m = BrowserManager(config=_cfg(max_contexts=1))
    await m.initialize()
    await m.get_context("s1")
    waiter = asyncio.create_task(m.get_context("s2", wait=True, timeout=5))
    await asyncio.sleep(0.01)
    assert "s2" in m._wait_queue
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert "s2" not in m._wait_queue and "s2" not in m._queue_order
    await m.release_context("s1", close=True)
    # nobody waits: no context was created for (and stranded under) s2
    assert "s2" not in m.contexts_in_use and len(made) == 1


@patch('tools.browser.browser_manager.Browser')
async def test_cleanup_then_initialize_restarts_background_tasks(mock_browser_class):
    mock_browser_class.return_value.new_context = AsyncMock(return_value=_ctx())
    mock_browser_class.return_value.close = AsyncMock()
    m = BrowserManager(config=_cfg())
    await m.initialize()
    await m.get_context("s1")
    assert m._queue_processor_task is not None
    await m.cleanup()
    assert m._queue_processor_task is None and m._cleanup_task is None
    await m.initialize()
    await m.get_context("s2")
    assert m._queue_processor_task is not None and not m._queue_processor_task.done()
    await m.cleanup()


@patch('tools.browser.browser_manager.Browser')
async def test_cleanup_resets_initialized(mock_browser_class):
    mock_browser_class.return_value.close = AsyncMock()
    m = BrowserManager(config=_cfg())
    await m.initialize()
    await m.cleanup()
    assert not m.is_initialized
    assert await m.get_browser() is not None  # re-initializes
    await m.cleanup()


async def test_two_sessions_of_one_agent_name_have_their_own_allowance():
    from tools.browser.browser_manager import _session_key
    assert _session_key("agent_sess-1") == "sess-1"
    assert _session_key("agent_sess-2") != _session_key("agent_sess-1")
    assert _session_key("sub-1-abc_sess-1") == "sess-1", "a child shares its parent's allowance"
    assert _session_key("plain") == "plain"


@patch('tools.browser.browser_manager.Browser')
async def test_a_context_still_closing_holds_its_slot(mock_browser_class):
    """A released context is out of the pool but not closed yet: it still counts, so
    the live Playwright contexts never exceed max_contexts while a close hangs."""
    hang = asyncio.Event()
    first = _ctx()

    async def slow_close():
        await hang.wait()
    first.close = slow_close
    made = [first]

    async def new_context(config=None, session_id=None):
        return made.pop(0) if made else _ctx()
    mock_browser_class.return_value.new_context = new_context
    m = BrowserManager(config=_cfg(max_contexts=1))
    await m.initialize()
    assert await m.get_context("s1") is first
    release = asyncio.create_task(m.release_context("s1"))
    await asyncio.sleep(0.01)
    assert m._slots_taken() == 1
    assert await m.get_context("s2", wait=False) is None   # the closing one holds the slot
    hang.set()
    await release
    assert m._slots_taken() == 0
    assert await m.get_context("s2", wait=False) is not None


@patch('tools.browser.browser_manager.Browser')
async def test_a_close_that_hangs_frees_its_slot_after_the_timeout(mock_browser_class, monkeypatch, caplog):
    """A close that truly hangs held its pool slot for ever: with max_contexts=1
    every later allocation waited for ever."""
    import tools.browser.browser_manager as bm
    monkeypatch.setattr(bm, "CONTEXT_CLOSE_TIMEOUT_SEC", 0.05)
    first = _ctx()

    async def hung_close():
        await asyncio.Event().wait()
    first.close = hung_close
    made = [first]

    async def new_context(config=None, session_id=None):
        return made.pop(0) if made else _ctx()
    mock_browser_class.return_value.new_context = new_context
    m = BrowserManager(config=_cfg(max_contexts=1))
    await m.initialize()
    assert await m.get_context("s1") is first
    await asyncio.wait_for(m.release_context("s1"), timeout=2)
    assert m._slots_taken() == 0
    assert await m.get_context("s2", wait=False) is not None
    assert any("did not close" in r.getMessage() for r in caplog.records)
