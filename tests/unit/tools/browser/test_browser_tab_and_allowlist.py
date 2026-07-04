import pytest
import inspect
from tools.browser.browser import Browser
from tools.browser import browser as browser_mod


def test_browser_tool_has_no_switch_to_tab_method_so_call_site_must_delegate():
    # Regression guard: browser.py:640 must NOT call self.switch_to_tab.
    # The Browser tool delegates tab-switching to its BrowserContext.
    assert not hasattr(Browser, "switch_to_tab"), (
        "Browser must not define switch_to_tab; the call site must use the context"
    )
    src = (Browser.__module__ and __import__("inspect").getsource(Browser))
    assert "self.switch_to_tab(" not in src, (
        "browser.py must not call self.switch_to_tab (AttributeError); "
        "delegate to browser_context.switch_to_tab instead"
    )


def test_go_to_url_enforces_allowed_domains_allowlist():
    src = inspect.getsource(browser_mod.Browser.go_to_url)
    # go_to_url must consult the context allowlist, not just the SSRF guard.
    assert "_is_url_allowed" in src or "navigate_to" in src, (
        "go_to_url must enforce BrowserContext allowed_domains "
        "(_is_url_allowed) before navigating"
    )
