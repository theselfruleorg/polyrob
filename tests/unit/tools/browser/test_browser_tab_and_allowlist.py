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
    # M3 (2026-09-14): the guard moved into the shared
    # Browser._pre_navigation_check, which open_tab now calls too — the verb
    # must route through it, and the helper must still consult the allowlist.
    src = inspect.getsource(browser_mod.Browser.go_to_url)
    assert "_pre_navigation_check" in src or "_is_url_allowed" in src, (
        "go_to_url must enforce BrowserContext allowed_domains "
        "(_is_url_allowed) before navigating"
    )
    helper = inspect.getsource(browser_mod.Browser._pre_navigation_check)
    assert "_is_url_allowed" in helper, (
        "the shared pre-navigation check must enforce allowed_domains")
