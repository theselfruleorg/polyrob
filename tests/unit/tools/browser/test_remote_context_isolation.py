"""Over a remote CDP/WSS browser every session gets a FRESH context.

Until 2026-09-17 the CDP path copied the desktop-attach branch and reused
``browser.contexts[0]`` — the remote service's default PERSISTENT context: the
X login's ``storage_state`` was silently dropped, service workers were not
blocked (a bypass of the SSRF route guard) and every session and tenant shared
one cookie jar that persisted on disk in the browser's user-data-dir.
"""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.browser.browser import BrowserConfig
from tools.browser.context import BrowserContext, BrowserContextConfig


@pytest.fixture(autouse=True)
def guard_on(monkeypatch):
    monkeypatch.delenv("BROWSER_ALLOW_PRIVATE_URLS", raising=False)


def _ctx(browser_config: BrowserConfig, **cfg):
    ctx = BrowserContext.__new__(BrowserContext)
    ctx.logger = logging.getLogger("test")
    ctx.session = None
    ctx.browser = SimpleNamespace(browser_config=browser_config)
    ctx.config = BrowserContextConfig(**cfg)
    return ctx


def _remote_browser():
    default_ctx = MagicMock(name="default-persistent-context")
    default_ctx.route = AsyncMock()
    default_ctx.add_init_script = AsyncMock()
    fresh = MagicMock(name="fresh-context")
    fresh.route = AsyncMock()
    fresh.add_init_script = AsyncMock()
    browser = SimpleNamespace(contexts=[default_ctx], new_context=AsyncMock(return_value=fresh))
    return browser, default_ctx, fresh


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["cdp_url", "wss_url"])
async def test_remote_browser_gets_a_fresh_context_with_login_and_sw_block(field):
    state = {"cookies": [{"name": "auth_token", "value": "x", "domain": ".x.com"}], "origins": []}
    ctx = _ctx(BrowserConfig(**{field: "ws://127.0.0.1:9222/x"}), storage_state=state)
    browser, default_ctx, fresh = _remote_browser()

    got = await ctx._create_context(browser, {})

    assert got is fresh
    kwargs = browser.new_context.call_args.kwargs
    assert kwargs["storage_state"] is state
    assert kwargs["service_workers"] == "block"
    default_ctx.route.assert_not_called()          # never touched
    default_ctx.add_init_script.assert_not_called()
    fresh.route.assert_awaited()                   # SSRF guard on the context we use


@pytest.mark.asyncio
async def test_desktop_attach_still_reuses_the_operator_profile():
    ctx = _ctx(BrowserConfig(chrome_instance_path="/usr/bin/google-chrome"))
    browser, default_ctx, fresh = _remote_browser()
    got = await ctx._create_context(browser, {})
    assert got is default_ctx
    browser.new_context.assert_not_called()


@pytest.mark.asyncio
async def test_new_page_reload_hack_is_desktop_only():
    """The 'reload every new page' hack was for pages inherited from a desktop
    Chrome; a fresh remote context must not re-navigate its own popups."""
    ctx = _ctx(BrowserConfig(cdp_url="http://127.0.0.1:9222"))
    handlers = {}
    context = SimpleNamespace(on=lambda ev, fn: handlers.__setitem__(ev, fn))
    ctx._add_new_page_listener(context)
    page = SimpleNamespace(reload=AsyncMock(), wait_for_load_state=AsyncMock(), url="about:blank")
    await handlers["page"](page)
    page.reload.assert_not_called()


@pytest.mark.asyncio
async def test_dead_remote_connection_is_replaced():
    """A remote browser that restarted must not poison the agent until IT restarts."""
    from tools.browser.browser import Browser
    tool = Browser(browser_config=BrowserConfig(cdp_url="http://127.0.0.1:9222",
                                                auto_configure_for_server=False))
    dead = SimpleNamespace(is_connected=lambda: False)
    old_driver = SimpleNamespace(stop=AsyncMock())
    tool._browser, tool._playwright = dead, old_driver
    fresh = object()
    tool._init = AsyncMock(side_effect=lambda: setattr(tool, "_browser", fresh) or fresh)
    assert await tool.get_playwright_browser() is fresh
    old_driver.stop.assert_awaited_once()
    tool._init.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_connection_is_reused():
    from tools.browser.browser import Browser
    tool = Browser(browser_config=BrowserConfig(cdp_url="http://127.0.0.1:9222",
                                                auto_configure_for_server=False))
    live = SimpleNamespace(is_connected=lambda: True)
    tool._browser = live
    tool._init = AsyncMock(side_effect=AssertionError("must not reconnect"))
    assert await tool.get_playwright_browser() is live


@pytest.mark.asyncio
async def test_dead_session_is_reinitialized():
    ctx = _ctx(BrowserConfig(cdp_url="http://127.0.0.1:9222"))
    ctx.session = SimpleNamespace(context=SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: False)))
    fresh = object()
    ctx._initialize_session = AsyncMock(return_value=fresh)
    assert await ctx.get_session() is fresh
    ctx._initialize_session.assert_awaited_once()
