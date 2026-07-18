"""Regression (P1 finalization): get_dropdown_options / select_dropdown_option called
`.locator` on the result of context.get_locate_element — but that returns a Playwright
ElementHandle, which has NO `.locator` attribute, so every call raised AttributeError
(silently surfaced as a generic tool error). They must use the ElementHandle API.
"""
import inspect

import tools.browser.browser as browser_mod


def test_elementhandle_api_assumptions_hold():
    """Guard the code's assumptions against the INSTALLED Playwright: the methods
    the dropdown handlers call exist, and `.locator` (the buggy access) does not."""
    from playwright.async_api import ElementHandle
    assert hasattr(ElementHandle, "query_selector_all")
    assert hasattr(ElementHandle, "select_option")
    assert hasattr(ElementHandle, "text_content")
    assert not hasattr(ElementHandle, "locator"), (
        "ElementHandle has no .locator — the dropdown handlers must not access it"
    )


def _owner():
    for _, obj in inspect.getmembers(browser_mod, inspect.isclass):
        if "get_dropdown_options" in getattr(obj, "__dict__", {}):
            return obj
    raise AssertionError("no class defines get_dropdown_options")


def test_dropdown_handlers_do_not_access_locate_result_locator():
    cls = _owner()
    for name in ("get_dropdown_options", "select_dropdown_option"):
        src = inspect.getsource(getattr(cls, name))
        assert "locate_result.locator" not in src, (
            f"{name} must not access .locator on the ElementHandle from get_locate_element"
        )
