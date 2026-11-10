"""The post receipt is the agent's OWN status, or an honest "not confirmed".

``_await_permalink`` swallowed a composer that never closed and returned the
FIRST ``/status/`` link on the page — another account's tweet on the timeline,
or a success line for a post that never went out.
"""
import pytest

pytest.importorskip("polyrob_x")

from polyrob_x.x_browser.driver import PostUnconfirmed, XPageDriver


class _El:
    def __init__(self, text=""):
        self.text = text

    async def inner_text(self):
        return self.text


class _Page:
    def __init__(self, *, composer_closes=True, links=None, handle="agent_acct"):
        self.composer_closes = composer_closes
        self.links = links or {}
        self.handle = handle
        self.asked = []

    async def wait_for_selector(self, selector, state=None, timeout=None):
        if state == "hidden" and not self.composer_closes:
            raise TimeoutError("composer still open")
        return _El()

    async def query_selector(self, selector):
        if "AccountSwitcher" in selector and self.handle:
            return _El(f"Agent Account\n@{self.handle}")
        return None

    async def get_attribute(self, selector, name):
        self.asked.append(selector)
        for needle, href in self.links.items():
            if needle in selector:
                return href
        return None


@pytest.mark.asyncio
async def test_composer_that_never_closed_is_not_a_post():
    d = XPageDriver(_Page(composer_closes=False,
                          links={"/status/": "/someone_else/status/9"}))
    with pytest.raises(PostUnconfirmed) as ei:
        await d._await_permalink(timeout=1)
    assert "not confirmed" in str(ei.value).lower()
    assert "twice" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_permalink_is_taken_only_from_the_own_handle():
    page = _Page(links={"/agent_acct/status/": "/agent_acct/status/77",
                        '"/status/"': "/someone_else/status/9"})
    url = await XPageDriver(page)._await_permalink(timeout=1)
    assert url == "https://x.com/agent_acct/status/77"
    assert all("/agent_acct/status/" in s for s in page.asked)


@pytest.mark.asyncio
async def test_no_own_link_returns_the_profile_not_a_stranger():
    page = _Page(links={'"/status/"': "/someone_else/status/9"})
    url = await XPageDriver(page)._await_permalink(timeout=1)
    assert url == "https://x.com/agent_acct"
    assert "someone_else" not in url


@pytest.mark.asyncio
async def test_unknown_handle_takes_no_status_link():
    page = _Page(handle="", links={'"/status/"': "/someone_else/status/9"})
    url = await XPageDriver(page)._await_permalink(timeout=1)
    assert "/status/" not in url
