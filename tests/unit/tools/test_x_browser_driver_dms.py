"""XPageDriver DM inbox/thread choreography without a live browser."""
import pytest

from tools.x_browser.driver import XPageDriver


class _Element:
    def __init__(self, text="", href=""):
        self.text = text
        self.href = href
        self.typed = ""
        self.clicked = False

    async def inner_text(self):
        return self.text

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def click(self):
        self.clicked = True

    async def type(self, text, delay=0):
        self.typed = text


class _Page:
    def __init__(self):
        self.urls = []
        self.rows = [
            _Element("Robinhood Alphas\nlatest reply", "/messages/11-99"),
            _Element("@whalecapitall\nsecond reply", "/messages/22-99"),
        ]
        self.messages = [_Element("sent hello"), _Element("inbound answer")]
        self.box = _Element()
        self.button = _Element()

    async def goto(self, url, wait_until=None):
        self.urls.append(url)

    async def wait_for_selector(self, selector, **kwargs):
        if "SideNav_NewTweet" in selector:
            return _Element()
        if "dmComposerTextInput" in selector:
            return self.box
        if "dmComposerSendButton" in selector:
            return self.button
        return _Element()

    async def query_selector_all(self, selector):
        if "messageEntry" in selector:
            return self.messages
        return self.rows

    async def inner_text(self, selector):
        return ""


@pytest.mark.asyncio
async def test_read_visible_inbox():
    driver = XPageDriver(_Page())
    result = await driver.read_dms(max_results=1)
    assert result["view"] == "inbox"
    assert len(result["conversations"]) == 1
    assert result["conversations"][0]["conversation_id"] == "11-99"


@pytest.mark.asyncio
async def test_read_thread_by_handle():
    driver = XPageDriver(_Page())
    result = await driver.read_dms("@whalecapitall", max_results=10)
    assert result["view"] == "thread"
    assert result["conversation"]["conversation_id"] == "22-99"
    assert result["messages"][-1]["text"] == "inbound answer"


@pytest.mark.asyncio
async def test_send_existing_thread():
    page = _Page()
    driver = XPageDriver(page)
    result = await driver.send_dm("Robinhood Alphas", "thanks")
    assert result == {"conversation_id": "11-99", "sent": True}
    assert page.box.typed == "thanks"
    assert page.button.clicked is True


@pytest.mark.asyncio
async def test_read_thread_by_conversation_id_navigates_directly():
    page = _Page()
    driver = XPageDriver(page)
    result = await driver.read_dms("22-99", max_results=10)
    assert result["conversation"]["conversation_id"] == "22-99"
    assert "https://x.com/messages/22-99" in page.urls


@pytest.mark.asyncio
async def test_inbox_selector_failure_is_not_reported_as_empty():
    class _BrokenInbox(_Page):
        async def wait_for_selector(self, selector, **kwargs):
            if "SideNav_NewTweet" in selector:
                return _Element()
            if 'href^="/messages/"' in selector:
                raise RuntimeError("selector drift")
            return await super().wait_for_selector(selector, **kwargs)

    with pytest.raises(RuntimeError, match="did not expose"):
        await XPageDriver(_BrokenInbox()).read_dms()


@pytest.mark.asyncio
async def test_rendered_empty_inbox_is_reported_as_empty():
    class _EmptyInbox(_Page):
        async def wait_for_selector(self, selector, **kwargs):
            if "SideNav_NewTweet" in selector:
                return _Element()
            if 'href^="/messages/"' in selector:
                raise RuntimeError("no rows")
            return await super().wait_for_selector(selector, **kwargs)

        async def inner_text(self, selector):
            return "Welcome to your inbox!"

    result = await XPageDriver(_EmptyInbox()).read_dms()
    assert result == {"view": "inbox", "conversations": []}
