from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tools.browser.browser import Browser, BrowserConfig
from tools.browser.launch_security import chromium_sandbox_enabled


@pytest.fixture(autouse=True)
def no_custody(monkeypatch):
    for name in ('POLYROB_LOCAL', 'AGENT_WALLET_ENABLED', 'AGENT_WALLET_MASTER_SEED', 'PAYMENT_MASTER_SEED', 'MASTER_SEED'):
        monkeypatch.delenv(name, raising=False)


def browser(**kwargs):
    return Browser(browser_config=BrowserConfig(auto_configure_for_server=False, **kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize('seed', ['AGENT_WALLET_MASTER_SEED', 'PAYMENT_MASTER_SEED', 'MASTER_SEED'])
async def test_custody_refuses_before_starting_driver(monkeypatch, seed):
    monkeypatch.setenv(seed, 'synthetic-test-credential')
    start = Mock(side_effect=AssertionError('must not start driver'))
    monkeypatch.setattr('tools.browser.browser.async_playwright', start)
    with pytest.raises(RuntimeError, match='custody'):
        await browser()._init()
    start.assert_not_called()


@pytest.mark.asyncio
async def test_existing_local_chrome_is_not_a_custody_bypass(monkeypatch):
    monkeypatch.setenv('AGENT_WALLET_ENABLED', 'true')
    probe = Mock(side_effect=AssertionError('must not connect to local chrome'))
    monkeypatch.setattr('requests.get', probe)
    with pytest.raises(RuntimeError, match='custody'):
        await browser(chrome_instance_path='/usr/bin/chromium')._setup_browser_with_instance(Mock())
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_standard_launch_explicitly_enables_chromium_sandbox():
    launch = AsyncMock(return_value=object())
    await browser()._setup_standard_browser(SimpleNamespace(chromium=SimpleNamespace(launch=launch)))
    assert launch.call_args.kwargs['chromium_sandbox'] is True


@pytest.mark.parametrize('argument', ['--no-sandbox', '--disable-setuid-sandbox', '--disable-seccomp-filter-sandbox=true'])
def test_extra_flags_cannot_silently_disable_sandbox(argument):
    with pytest.raises(ValueError, match='opt-in'):
        chromium_sandbox_enabled(BrowserConfig(), [argument])


def test_explicit_non_custody_development_opt_out(monkeypatch):
    monkeypatch.setenv('POLYROB_LOCAL', 'true')
    assert chromium_sandbox_enabled(BrowserConfig(use_no_sandbox=True), ['--no-sandbox']) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('method,config,connect', [
    ('_setup_cdp', 'cdp_url', 'connect_over_cdp'),
    ('_setup_wss', 'wss_url', 'connect'),
])
async def test_remote_endpoint_secrets_do_not_reach_logs_or_error(method, config, connect, caplog):
    endpoint = 'wss://user:secret-value@example.test/browser?token=secret-value'
    tool = browser(**{config: endpoint})
    chromium = SimpleNamespace(**{connect: AsyncMock(side_effect=RuntimeError(endpoint))})
    with pytest.raises(Exception) as raised:
        await getattr(tool, method)(SimpleNamespace(chromium=chromium))
    assert 'secret-value' not in str(raised.value)
    assert 'secret-value' not in caplog.text


@pytest.mark.asyncio
async def test_headless_retry_keeps_sandbox_enabled():
    tool = browser()
    tool.browser_config.headless = False
    launch = AsyncMock(side_effect=[RuntimeError('display unavailable'), object()])
    await tool._setup_standard_browser(SimpleNamespace(chromium=SimpleNamespace(launch=launch)))
    assert len(launch.call_args_list) == 2
    assert all(call.kwargs['chromium_sandbox'] is True for call in launch.call_args_list)


@pytest.mark.asyncio
async def test_failed_setup_stops_driver(monkeypatch):
    tool = browser()
    driver = SimpleNamespace(stop=AsyncMock())
    monkeypatch.setattr('tools.browser.browser.async_playwright',
                        lambda: SimpleNamespace(start=AsyncMock(return_value=driver)))
    tool._setup_browser = AsyncMock(side_effect=RuntimeError('launch failed'))
    with pytest.raises(RuntimeError, match='launch failed'):
        await tool._init()
    driver.stop.assert_awaited_once()
    assert tool._playwright is None


def test_server_cannot_opt_out_of_chromium_sandbox():
    with pytest.raises(ValueError, match="local, non-custody"):
        chromium_sandbox_enabled(BrowserConfig(use_no_sandbox=True), [])


# --- owner-ceremony launches (049 phase 4) -------------------------------------

def test_desktop_launch_refused_under_custody(monkeypatch):
    from tools.browser.launch_security import desktop_launch_kwargs
    monkeypatch.setenv('AGENT_WALLET_ENABLED', 'true')
    with pytest.raises(RuntimeError, match='custody'):
        desktop_launch_kwargs(headless=False)


def test_desktop_launch_is_scrubbed_and_sandboxed(monkeypatch):
    from tools.browser.launch_security import desktop_launch_kwargs
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-secret')
    monkeypatch.setenv('DISPLAY', ':0')
    kw = desktop_launch_kwargs(headless=False, args=['--disable-gpu'])
    assert kw['chromium_sandbox'] is True and kw['headless'] is False
    assert 'OPENAI_API_KEY' not in kw['env'] and kw['env'].get('DISPLAY') == ':0'
    assert kw['args'] == ['--disable-gpu']
