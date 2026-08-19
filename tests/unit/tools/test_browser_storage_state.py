"""BrowserContextConfig.storage_state pass-through + export (Task 7, 2026-08-18 plan)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.browser.context import BrowserContext, BrowserContextConfig


def _fake_pw_browser(captured):
    ctx = MagicMock()
    ctx.route = AsyncMock()
    ctx.add_init_script = AsyncMock()
    ctx.add_cookies = AsyncMock()

    async def new_context(**kwargs):
        captured.update(kwargs)
        return ctx

    pw_browser = MagicMock()
    pw_browser.contexts = []
    pw_browser.new_context = new_context
    return pw_browser, ctx


def _fake_browser():
    browser = MagicMock()
    browser.config.cdp_url = None
    browser.config.chrome_instance_path = None
    return browser


@pytest.mark.asyncio
async def test_storage_state_passed_to_new_context():
    captured = {}
    pw_browser, _ = _fake_pw_browser(captured)
    state = {"cookies": [{"name": "auth", "value": "x", "domain": ".x.com"}]}
    bc = BrowserContext(browser=_fake_browser(),
                        config=BrowserContextConfig(storage_state=state))
    await bc._create_context(pw_browser, {})
    assert captured["storage_state"] == state


@pytest.mark.asyncio
async def test_no_storage_state_keeps_kwargs_clean():
    captured = {}
    pw_browser, _ = _fake_pw_browser(captured)
    bc = BrowserContext(browser=_fake_browser(), config=BrowserContextConfig())
    await bc._create_context(pw_browser, {})
    assert "storage_state" not in captured


@pytest.mark.asyncio
async def test_export_storage_state_reads_playwright_context():
    bc = BrowserContext(browser=_fake_browser(), config=BrowserContextConfig())
    fake_session = MagicMock()
    fake_session.context.storage_state = AsyncMock(
        return_value={"cookies": [], "origins": []})

    async def get_session():
        return fake_session

    bc.get_session = get_session
    out = await bc.export_storage_state()
    assert out == {"cookies": [], "origins": []}
