"""A screenshot of a page that was never navigated carries no information.

Prod 2026-09-19: every step of every money-rail run (EXIT/SAFETY/WATCHER/SCOUT
— none of which browse) logged `📷 VISION ENABLED: Sending 1 image(s)` from
step 1, because the session's fresh browser context sits on `about:blank` and
`get_state(capture_screenshot=True)` dutifully shoots it. That image rode on
every call through the vision path of the metered seat for nothing. The prompt
render is the ONE place the image is attached, so it is the one place to skip
a blank page; a navigated page still ships its screenshot unchanged.
"""
from agents.task.agent.prompts import AgentMessagePrompt, is_blank_page_url


class _Tree:
    def clickable_elements_to_string(self, include_attributes=None):
        return ""


class _State:
    def __init__(self, url, screenshot="iVBORw0KGgo="):
        self.url = url
        self.title = ""
        self.tabs = []
        self.screenshot = screenshot
        self.pixels_above = 0
        self.pixels_below = 0
        self.element_tree = _Tree()


def _content(url, use_vision=True, screenshot="iVBORw0KGgo="):
    return AgentMessagePrompt(state=_State(url, screenshot)).get_user_message(
        use_vision=use_vision).content


def test_blank_page_url_predicate():
    for u in ("", "about:blank", "ABOUT:BLANK", "  about:blank ", None, "chrome://newtab", "chrome://new-tab-page/"):
        assert is_blank_page_url(u), u
    for u in ("https://x.com/home", "file:///tmp/a.html", "about:srcdoc"):
        assert not is_blank_page_url(u), u


def test_blank_page_screenshot_is_not_attached():
    for url in ("", "about:blank"):
        content = _content(url)
        assert isinstance(content, str), f"blank page {url!r} must render text-only"
        assert "image_url" not in content


def test_navigated_page_screenshot_still_attached():
    content = _content("https://x.com/home")
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_off_stays_text_only_on_a_navigated_page():
    assert isinstance(_content("https://x.com/home", use_vision=False), str)
