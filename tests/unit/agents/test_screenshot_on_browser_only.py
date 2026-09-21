"""057 WS-B: capture a screenshot only on a step that used the browser.

`browser` is in AUTONOMOUS_MODE_TOOLS, so every autonomous session holds a
browser context and get_state captured a screenshot on EVERY step — including
the large majority that never touch a page. The 2026-09-19 fix stopped SENDING
the image; the capture still ran.
"""
import pytest

from agents.task.agent.core.step import (
    _screenshot_on_browser_only, _should_capture_screenshot,
)


class _Agent:
    def __init__(self, used=False, raises=False):
        self._used = used
        self._raises = raises

    def _has_active_browser_usage(self):
        if self._raises:
            raise RuntimeError("probe blew up")
        return self._used


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_SCREENSHOT_ON_BROWSER_ONLY", raising=False)


def test_off_by_default():
    assert _screenshot_on_browser_only() is False


def test_vision_off_never_captures():
    assert _should_capture_screenshot(_Agent(used=True), False) is False


def test_default_captures_whenever_vision_is_on():
    assert _should_capture_screenshot(_Agent(used=False), True) is True


def test_flag_on_skips_a_step_that_never_touched_a_page(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_SCREENSHOT_ON_BROWSER_ONLY", "true")
    assert _should_capture_screenshot(_Agent(used=False), True) is False


def test_flag_on_still_captures_a_browser_step(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_SCREENSHOT_ON_BROWSER_ONLY", "true")
    assert _should_capture_screenshot(_Agent(used=True), True) is True


def test_a_failing_probe_fails_open(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_SCREENSHOT_ON_BROWSER_ONLY", "true")
    # A missing screenshot on a page-driving step is worse than a spare one.
    assert _should_capture_screenshot(_Agent(raises=True), True) is True
