"""Playwright 1.63 (pinned in requirements.lock) has no `page.accessibility`; every
extract fell back to raw HTML (prod 2026-10-04 11:01, "'Page' object has no
attribute 'accessibility'"). The replacement is `locator.aria_snapshot()`."""
import asyncio

from tools.browser.browser import _semantic_snapshot


class _Locator:
    def __init__(self, text):
        self._text = text

    async def aria_snapshot(self):
        return self._text


class _ModernPage:
    """Playwright >= 1.49 shape: no `.accessibility`, has aria_snapshot."""

    def __init__(self, text):
        self._text = text
        self.asked = None

    def locator(self, selector):
        self.asked = selector
        return _Locator(self._text)


class _LegacyAccessibility:
    async def snapshot(self):
        return {"role": "WebArea", "name": "old", "children": []}


class _LegacyPage:
    accessibility = _LegacyAccessibility()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_modern_page_uses_aria_snapshot_of_the_body():
    page = _ModernPage('- textbox "Email"\n- button "Register"')
    text, method = _run(_semantic_snapshot(page, format_tree=lambda n: ["unused"]))
    assert page.asked == "body"
    assert 'textbox "Email"' in text
    assert method == "aria_snapshot"


def test_legacy_page_keeps_the_accessibility_tree_path():
    text, method = _run(_semantic_snapshot(_LegacyPage(), format_tree=lambda n: ["- WebArea old"]))
    assert text == "- WebArea old"
    assert method == "accessibility_snapshot"


def test_empty_snapshot_returns_nothing_so_the_caller_falls_back():
    text, _ = _run(_semantic_snapshot(_ModernPage(""), format_tree=lambda n: []))
    assert not text
