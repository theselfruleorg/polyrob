"""M3 (wallet-security evaluation, 2026-09-14): `open_tab` gets the same
pre-navigation guards as `go_to_url`.

``go_to_url`` refused ``file://`` and consulted the operator's
``allowed_domains`` allowlist; ``open_tab`` did neither. One prompt-injected
``browser_open_tab(url="file:///etc/polyrob/polyrob.env")`` would have loaded the
file that holds ``AGENT_WALLET_MASTER_SEED`` and the approval flags into a page
the agent then reads.

Both verbs now route through ONE ``Browser._pre_navigation_check``, so the two
can no longer drift apart. These tests drive the real actions (undecorated, with
a stub context) rather than asserting on source text, so a future refactor that
drops the call fails here.
"""
import asyncio
import inspect
import logging

import pytest

from tools.browser.actions import GoToUrlAction, OpenTabAction
from tools.browser.browser import Browser

_PARAM_MODEL = {"go_to_url": GoToUrlAction, "open_tab": OpenTabAction}

SEED_FILE = "file:///etc/polyrob/polyrob.env"


class _StubContext:
    """Minimal BrowserContext stand-in: records whether navigation happened."""

    def __init__(self, allowed=True):
        self._allowed = allowed
        self.navigated = False

    def _is_url_allowed(self, url):  # noqa: D401 - mirrors the real signature
        return self._allowed

    async def get_current_page(self):
        self.navigated = True
        raise AssertionError("navigation must never be reached for a refused URL")

    @property
    def session(self):
        self.navigated = True
        raise AssertionError("navigation must never be reached for a refused URL")


class _Ctx:
    def __init__(self, browser_context):
        self.browser_context = browser_context


def _browser():
    b = object.__new__(Browser)
    b.logger = logging.getLogger("browser-nav-test")
    return b


def _undecorated(name):
    """The raw coroutine behind the @BaseTool.action wrapper, if wrapped."""
    fn = getattr(Browser, name)
    return getattr(fn, "__wrapped__", fn)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------

def test_helper_exists_and_is_a_coroutine():
    assert inspect.iscoroutinefunction(Browser._pre_navigation_check)


@pytest.mark.parametrize("url", [
    SEED_FILE,
    "file:///etc/passwd",
    "http://x/redirect?to=file:///etc/polyrob/polyrob.env",  # embedded file:///
])
def test_helper_refuses_file_urls(url, monkeypatch):
    monkeypatch.delenv("BROWSER_ALLOW_PRIVATE_URLS", raising=False)
    res = _run(_browser()._pre_navigation_check(url, _StubContext()))
    assert res is not None and res.error
    assert "file://" in res.error


def test_helper_does_not_treat_a_data_url_as_a_file_url(monkeypatch):
    """`data:` is how the agent renders a generated image — the file:// rung
    must not claim it. (It is still subject to the SSRF rung, exactly as it was
    before the extraction; this pins only that the refusal is not the file one.)"""
    monkeypatch.delenv("BROWSER_ALLOW_PRIVATE_URLS", raising=False)
    res = _run(_browser()._pre_navigation_check("data:image/png;base64,AAAA", _StubContext()))
    assert res is None or "file://" not in (res.error or "")


def test_helper_refuses_a_disallowed_domain(monkeypatch):
    monkeypatch.delenv("BROWSER_ALLOW_PRIVATE_URLS", raising=False)
    res = _run(_browser()._pre_navigation_check(
        "https://93.184.216.34/", _StubContext(allowed=False)))
    assert res is not None and "allowed domains list" in res.error


def test_helper_refuses_cloud_metadata(monkeypatch):
    monkeypatch.delenv("BROWSER_ALLOW_PRIVATE_URLS", raising=False)
    res = _run(_browser()._pre_navigation_check(
        "http://169.254.169.254/latest/meta-data/", _StubContext()))
    assert res is not None and "SSRF" in res.error


# ---------------------------------------------------------------------------
# Both verbs route through it — the actual M3 regression
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["go_to_url", "open_tab"])
def test_both_navigation_verbs_refuse_the_env_file(action, monkeypatch):
    monkeypatch.delenv("BROWSER_ALLOW_PRIVATE_URLS", raising=False)
    ctx = _StubContext()
    params = _PARAM_MODEL[action](url=SEED_FILE)
    res = _run(_undecorated(action)(_browser(), params, _Ctx(ctx)))
    assert res is not None and res.error, f"{action} did not refuse {SEED_FILE}"
    assert "file://" in res.error, f"{action}: {res.error}"
    assert ctx.navigated is False


@pytest.mark.parametrize("action", ["go_to_url", "open_tab"])
def test_both_navigation_verbs_honour_the_domain_allowlist(action, monkeypatch):
    monkeypatch.delenv("BROWSER_ALLOW_PRIVATE_URLS", raising=False)
    ctx = _StubContext(allowed=False)
    params = _PARAM_MODEL[action](url="https://93.184.216.34/")
    res = _run(_undecorated(action)(_browser(), params, _Ctx(ctx)))
    assert res is not None and res.error
    assert "allowed domains list" in res.error, f"{action}: {res.error}"
    assert ctx.navigated is False


@pytest.mark.parametrize("action", ["go_to_url", "open_tab"])
def test_both_navigation_verbs_call_the_shared_helper(action):
    """Source-level backstop: neither verb may reintroduce its own guard set."""
    src = inspect.getsource(_undecorated(action))
    assert "_pre_navigation_check" in src, (
        f"{action} must route through Browser._pre_navigation_check")
