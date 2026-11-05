"""A failed init must not leak its context into the long-lived CDP Chromium, a close must
not hang on a dead browser, and a slow close must not hold the pool lock.

``page.close(timeout=)`` raised TypeError on every Playwright this repo has pinned, and
the debug log hid it, so the page step did nothing."""
import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import BotConfig
from tools.browser import context as context_module
from tools.browser.browser_manager import BrowserManager
from tools.browser.context import BrowserContext, BrowserContextConfig

pytestmark = pytest.mark.asyncio


async def _hang(*_a, **_k):
    await asyncio.sleep(60)


def _pw_context():
    ctx = MagicMock()
    ctx.pages = []
    for name in ("route", "add_init_script", "new_page", "close", "add_cookies", "cookies"):
        setattr(ctx, name, AsyncMock())
    ctx.tracing.stop = AsyncMock()
    ctx._impl_obj.close = AsyncMock()
    return ctx


def _browser_context(monkeypatch, pw_ctx, *, chrome_path=None, existing=()):
    monkeypatch.setattr("tools.browser._flags.allow_private_urls", lambda: False)
    pw = SimpleNamespace(new_context=AsyncMock(return_value=pw_ctx), contexts=list(existing))
    browser = SimpleNamespace(
        browser_config=SimpleNamespace(chrome_instance_path=chrome_path),
        get_playwright_browser=AsyncMock(return_value=pw))
    return BrowserContext(browser=browser, config=BrowserContextConfig())


async def test_a_failure_after_creation_closes_the_context(monkeypatch):
    pw_ctx = _pw_context()
    pw_ctx.new_page.side_effect = RuntimeError("target crashed")
    ctx = _browser_context(monkeypatch, pw_ctx)
    with pytest.raises(RuntimeError, match="target crashed"):
        await ctx._initialize_session()
    pw_ctx.close.assert_awaited_once()
    assert ctx.session is None


@pytest.mark.parametrize("step", ["route", "add_init_script"])
async def test_a_failure_inside_create_context_closes_the_context(monkeypatch, step):
    pw_ctx = _pw_context()
    getattr(pw_ctx, step).side_effect = RuntimeError("step failed")
    ctx = _browser_context(monkeypatch, pw_ctx)
    with pytest.raises(RuntimeError, match="step failed"):
        await ctx._initialize_session()
    pw_ctx.close.assert_awaited_once()


async def test_a_cancelled_init_closes_the_context(monkeypatch):
    pw_ctx = _pw_context()
    pw_ctx.new_page.side_effect = _hang
    ctx = _browser_context(monkeypatch, pw_ctx)
    task = asyncio.create_task(ctx._initialize_session())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pw_ctx.close.assert_awaited_once()


async def test_a_hung_close_of_the_failed_context_is_bounded(monkeypatch):
    monkeypatch.setattr(context_module, "_CLOSE_STEP_S", 0.01)
    pw_ctx = _pw_context()
    pw_ctx.new_page.side_effect = RuntimeError("target crashed")
    pw_ctx.close.side_effect = _hang
    ctx = _browser_context(monkeypatch, pw_ctx)
    with pytest.raises(RuntimeError, match="target crashed"):
        await asyncio.wait_for(ctx._initialize_session(), timeout=2)


async def test_the_operators_default_context_is_never_closed_on_failure(monkeypatch):
    own = _pw_context()
    own.new_page.side_effect = RuntimeError("target crashed")
    ctx = _browser_context(monkeypatch, _pw_context(), chrome_path="/usr/bin/chrome", existing=[own])
    with pytest.raises(RuntimeError, match="target crashed"):
        await ctx._initialize_session()
    own.close.assert_not_awaited()


async def test_a_new_context_in_the_operators_chrome_is_closed_on_failure(monkeypatch):
    pw_ctx = _pw_context()
    pw_ctx.new_page.side_effect = RuntimeError("target crashed")
    ctx = _browser_context(monkeypatch, pw_ctx, chrome_path="/usr/bin/chrome")
    with pytest.raises(RuntimeError, match="target crashed"):
        await ctx._initialize_session()
    pw_ctx.close.assert_awaited_once()


async def test_a_successful_init_keeps_the_context_open(monkeypatch):
    pw_ctx = _pw_context()
    ctx = _browser_context(monkeypatch, pw_ctx)
    session = await ctx._initialize_session()
    assert session.context is pw_ctx
    pw_ctx.close.assert_not_awaited()
    assert ctx._fresh_context is None
    ctx.session = None


class _Page:
    """The signature of the pinned Playwright: there is no ``timeout`` keyword."""

    def __init__(self, hang=False):
        self.closed = False
        self._hang = hang

    async def close(self, *, reason=None, run_before_unload=None):
        if self._hang:
            await _hang()
        self.closed = True


def _live(monkeypatch, pw_ctx):
    monkeypatch.setattr(context_module, "_CLOSE_STEP_S", 0.01)
    ctx = _browser_context(monkeypatch, pw_ctx)
    ctx.session = SimpleNamespace(context=pw_ctx)
    return ctx


async def test_close_really_closes_the_pages(monkeypatch):
    pw_ctx = _pw_context()
    pw_ctx.pages = [_Page(), _Page(hang=True), _Page()]
    ctx = _live(monkeypatch, pw_ctx)
    await asyncio.wait_for(ctx.close(), timeout=2)
    assert [p.closed for p in pw_ctx.pages] == [True, False, True]
    pw_ctx.close.assert_awaited_once()
    assert ctx.session is None


async def test_keep_alive_keeps_the_pages(monkeypatch):
    pw_ctx = _pw_context()
    pw_ctx.pages = [_Page()]
    ctx = _live(monkeypatch, pw_ctx)
    ctx.config = BrowserContextConfig(_force_keep_context_alive=True)
    await ctx.close()
    assert not pw_ctx.pages[0].closed
    pw_ctx.close.assert_not_awaited()


async def test_page_close_has_no_timeout_keyword_on_the_installed_playwright():
    api = pytest.importorskip("playwright.async_api")
    assert "timeout" not in inspect.signature(api.Page.close).parameters


async def test_hung_cookie_and_trace_steps_do_not_stop_the_close(monkeypatch):
    pw_ctx = _pw_context()
    pw_ctx.tracing.stop.side_effect = _hang
    ctx = _live(monkeypatch, pw_ctx)
    monkeypatch.setattr(context_module, "_CONTEXT_CLOSE_S", 0.01)
    ctx.config = BrowserContextConfig(trace_path="/tmp")
    ctx.save_cookies = _hang
    await asyncio.wait_for(ctx.close(), timeout=2)
    pw_ctx.close.assert_awaited_once()


@pytest.mark.parametrize("failure", [_hang, RuntimeError("close failed")])
async def test_the_force_close_is_bounded(monkeypatch, failure):
    pw_ctx = _pw_context()
    pw_ctx.close.side_effect = failure if isinstance(failure, Exception) else _hang
    pw_ctx._impl_obj.close.side_effect = _hang
    ctx = _live(monkeypatch, pw_ctx)
    monkeypatch.setattr(context_module, "_CONTEXT_CLOSE_S", 0.01)
    await asyncio.wait_for(ctx.close(), timeout=2)
    pw_ctx._impl_obj.close.assert_awaited_once()
    assert ctx.session is None


def _manager():
    cfg = BotConfig()
    cfg.browser_stale_timeout = 60.0
    m = BrowserManager(config=cfg)
    m._ensure_background_tasks_started = AsyncMock()
    return m


class _SlowContext:
    def __init__(self, manager):
        self.manager = manager
        self.lock_held_during_close = None

    async def close(self):
        self.lock_held_during_close = self.manager._pool_lock.locked()


async def test_release_closes_outside_the_pool_lock():
    m = _manager()
    ctx = _SlowContext(m)
    m.contexts_in_use["s1"] = ctx
    m.context_allocation_times["s1"] = 0
    m.session_contexts["s1"] = ["s1"]
    await m.release_context("s1")
    assert ctx.lock_held_during_close is False
    assert "s1" not in m.contexts_in_use and "s1" not in m.session_contexts


async def test_a_second_release_of_the_session_does_nothing():
    m = _manager()
    ctx = MagicMock(close=AsyncMock())
    m.contexts_in_use["s1"] = ctx
    await asyncio.gather(m.release_context("s1"), m.release_context("s1"))
    ctx.close.assert_awaited_once()


async def test_the_reaper_closes_outside_the_pool_lock():
    m = _manager()
    ctx = _SlowContext(m)
    m.contexts_in_use["s1"] = ctx
    m.context_allocation_times["s1"] = 0
    await m._reap_stale_contexts_once()
    assert ctx.lock_held_during_close is False
    assert "s1" not in m.contexts_in_use


async def test_a_release_serves_the_wait_queue_after_the_close():
    m = _manager()
    order = []
    ctx = MagicMock()

    async def _close():
        order.append("close")
    ctx.close = _close

    async def _notify():
        assert m._pool_lock.locked()
        order.append("notify")
    m._notify_wait_queue = _notify
    m.contexts_in_use["s1"] = ctx
    await m.release_context("s1")
    assert order == ["close", "notify"]


async def test_a_hung_close_does_not_block_another_session():
    m = _manager()
    closing = asyncio.Event()

    async def _close():
        closing.set()
        await _hang()
    m.contexts_in_use["s1"] = MagicMock(close=_close)
    other = MagicMock()
    m.contexts_in_use["s2"] = other
    release = asyncio.create_task(m.release_context("s1"))
    await closing.wait()
    assert await asyncio.wait_for(m.get_context("s2"), timeout=1) is other
    release.cancel()


async def test_a_cancelled_reaper_still_closes_every_detached_context():
    """They left the pool, so nothing else can reach them after a cancel."""
    m = _manager()
    gate = asyncio.Event()

    async def _close():
        await gate.wait()
    contexts = [MagicMock(close=AsyncMock(side_effect=_close)) for _ in range(3)]
    for i, ctx in enumerate(contexts):
        m.contexts_in_use[f"s{i}"] = ctx
        m.context_allocation_times[f"s{i}"] = 0
    task = asyncio.create_task(m._reap_stale_contexts_once())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await asyncio.sleep(0.01)
    for ctx in contexts:
        ctx.close.assert_awaited_once()
