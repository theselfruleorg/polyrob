"""x_browser tool — registration contract + x_post / x_login_check (Task 9)."""
import pytest

from core.config import BotConfig


# --- registration + capability contract -----------------------------------

def test_gate_off_not_registered(monkeypatch):
    monkeypatch.delenv("X_BROWSER_ENABLED", raising=False)
    from tools.x_browser.registration import x_browser_enabled
    assert x_browser_enabled() is False


def test_gate_on(monkeypatch):
    monkeypatch.setenv("X_BROWSER_ENABLED", "true")
    from tools.x_browser.registration import x_browser_enabled
    assert x_browser_enabled() is True


def test_capability_classification():
    from core.tool_capabilities import TOOL_CAPABILITIES, is_classified
    assert is_classified("x_browser")
    caps = TOOL_CAPABILITIES["x_browser"]
    assert "high_impact" in caps and "delegate_blocked" in caps


def test_approval_gating_lists():
    from tools.controller.approval import (
        DEFAULT_APPROVAL_REQUIRED_TOOLS,
        _ALWAYS_GATED_VERBS,
    )
    assert "x_browser_x_post" in DEFAULT_APPROVAL_REQUIRED_TOOLS
    assert "x_browser_x_dm" in DEFAULT_APPROVAL_REQUIRED_TOOLS
    assert "x_browser_x_signup_start" in _ALWAYS_GATED_VERBS


# --- x_post / x_login_check behaviour -------------------------------------

class FakeDriver:
    def __init__(self, *, logged_in=True, url="https://x.com/robbot/status/1"):
        self._logged_in = logged_in
        self._url = url
        self.posted = None
        self.dm_reads = []
        self.dm_sends = []

    async def is_logged_in(self):
        return self._logged_in

    async def post(self, text):
        if not self._logged_in:
            raise RuntimeError("not logged in")
        self.posted = text
        return self._url

    async def read_dms(self, participant, max_results):
        self.dm_reads.append((participant, max_results))
        return {"view": "thread", "messages": [{"text": "inbound hello"}]}

    async def send_dm(self, participant, text):
        self.dm_sends.append((participant, text))
        return {"conversation_id": "42-999", "sent": True}


def _tool(monkeypatch, *, session=None, driver=None):
    monkeypatch.setenv("X_BROWSER_ENABLED", "true")
    from tools.x_browser.tool import XBrowserTool
    tool = XBrowserTool("x_browser", BotConfig(), None)

    class _Store:
        def load(self, uid):
            return session

        def exists(self, uid):
            return session is not None
    tool._session_store = _Store()

    async def _open(user_id):
        if driver is None:
            raise AssertionError("driver not provided")
        return driver, (lambda: None)
    tool._open_driver = _open
    return tool


class _Ctx:
    def __init__(self, user_id="u1", role="orchestrator"):
        self.user_id = user_id
        self.session_id = "s1"
        self.role = role
        self.is_sub_agent = False
        self.metadata = {}


@pytest.mark.asyncio
async def test_x_post_no_session_is_structured_error(monkeypatch):
    from tools.x_browser.tool import XPostAction
    tool = _tool(monkeypatch, session=None)
    res = await tool.x_post(XPostAction(text="hi"), execution_context=_Ctx())
    assert res.error and "no x session" in res.error.lower()


@pytest.mark.asyncio
async def test_x_post_success_returns_url(monkeypatch):
    from tools.x_browser.tool import XPostAction
    driver = FakeDriver(url="https://x.com/robbot/status/42")
    tool = _tool(monkeypatch, session={"handle": "robbot", "storage_state": {}},
                 driver=driver)
    res = await tool.x_post(XPostAction(text="hello world"), execution_context=_Ctx())
    assert res.error is None
    assert "https://x.com/robbot/status/42" in res.extracted_content
    assert driver.posted == "hello world"


@pytest.mark.asyncio
async def test_x_post_over_280_rejected(monkeypatch):
    from pydantic import ValidationError

    from tools.x_browser.tool import XPostAction
    with pytest.raises(ValidationError):
        XPostAction(text="x" * 281)


@pytest.mark.asyncio
async def test_x_login_check_expired(monkeypatch):
    driver = FakeDriver(logged_in=False)
    tool = _tool(monkeypatch, session={"handle": "robbot", "storage_state": {}},
                 driver=driver)
    res = await tool.x_login_check(execution_context=_Ctx())
    assert res.error is None
    assert "capture-session" in res.extracted_content or "re-capture" in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_x_post_refused_on_leaf_turn(monkeypatch):
    from tools.x_browser.tool import XPostAction
    driver = FakeDriver()
    tool = _tool(monkeypatch, session={"handle": "robbot", "storage_state": {}},
                 driver=driver)
    res = await tool.x_post(XPostAction(text="hi"), execution_context=_Ctx(role="leaf"))
    assert res.error and "delegated" in res.error.lower()


@pytest.mark.asyncio
async def test_x_read_dms_reads_visible_thread(monkeypatch):
    from tools.x_browser.tool import XReadDMsAction
    driver = FakeDriver()
    tool = _tool(monkeypatch, session={"storage_state": {}}, driver=driver)
    res = await tool.x_read_dms(
        XReadDMsAction(participant="@alice", max_results=7),
        execution_context=_Ctx())
    assert res.error is None
    assert "inbound hello" in res.extracted_content
    assert driver.dm_reads == [("@alice", 7)]


@pytest.mark.asyncio
async def test_x_read_dms_requires_captured_session(monkeypatch):
    from tools.x_browser.tool import XReadDMsAction
    tool = _tool(monkeypatch, session=None)
    res = await tool.x_read_dms(XReadDMsAction(), execution_context=_Ctx())
    assert res.error and "x-account capture-session" in res.error


@pytest.mark.asyncio
async def test_x_dm_sends_existing_thread(monkeypatch):
    from tools.x_browser.tool import XDMAction
    driver = FakeDriver()
    tool = _tool(monkeypatch, session={"storage_state": {}}, driver=driver)
    res = await tool.x_dm(
        XDMAction(participant="alice", text="hello"),
        execution_context=_Ctx())
    assert res.error is None
    assert "42-999" in res.extracted_content
    assert driver.dm_sends == [("alice", "hello")]


@pytest.mark.asyncio
async def test_x_dm_refused_on_leaf_turn(monkeypatch):
    from tools.x_browser.tool import XDMAction
    tool = _tool(monkeypatch, session={"storage_state": {}}, driver=FakeDriver())
    res = await tool.x_dm(
        XDMAction(participant="alice", text="hello"),
        execution_context=_Ctx(role="leaf"))
    assert res.error and "delegated" in res.error.lower()


# --- x_signup_start (Task 13) ---------------------------------------------

class FakeSignupFlow:
    def __init__(self, *, raise_paused=None, result=None):
        self._raise = raise_paused
        self._result = result

    async def run(self, resume=False):
        if self._raise is not None:
            raise self._raise
        return self._result


@pytest.mark.asyncio
async def test_signup_refused_on_forged_turn(monkeypatch):
    from tools.x_browser.tool import XSignupStartAction
    tool = _tool(monkeypatch, session=None)
    res = await tool.x_signup_start(
        XSignupStartAction(), execution_context=_Ctx(role="leaf"))
    assert res.error and ("delegated" in res.error.lower() or "forged" in res.error.lower())


@pytest.mark.asyncio
async def test_signup_refused_when_account_exists(monkeypatch):
    from tools.x_browser.signup import Obstacle, SignupPaused
    from tools.x_browser.tool import XSignupStartAction
    tool = _tool(monkeypatch, session={"handle": "already", "storage_state": {}})

    async def _flow(user_id, resume):
        return FakeSignupFlow(
            raise_paused=SignupPaused(Obstacle.BLOCKED, "already exists"))
    tool._build_signup_flow = _flow

    res = await tool.x_signup_start(XSignupStartAction(), execution_context=_Ctx())
    assert res.error and "already" in res.error.lower()


@pytest.mark.asyncio
async def test_signup_success_returns_handle(monkeypatch):
    from tools.x_browser.signup import SignupResult
    from tools.x_browser.tool import XSignupStartAction
    tool = _tool(monkeypatch, session=None)

    async def _flow(user_id, resume):
        return FakeSignupFlow(
            result=SignupResult(handle="robbot", address="rob@agentmail.to"))
    tool._build_signup_flow = _flow

    res = await tool.x_signup_start(XSignupStartAction(), execution_context=_Ctx())
    assert res.error is None
    assert "robbot" in res.extracted_content


@pytest.mark.asyncio
async def test_signup_paused_reports_to_owner(monkeypatch):
    from tools.x_browser.escalation import EscalationOutcome
    from tools.x_browser.signup import Obstacle, SignupPaused
    from tools.x_browser.tool import XSignupStartAction
    tool = _tool(monkeypatch, session=None)

    async def _flow(user_id, resume):
        return FakeSignupFlow(
            raise_paused=SignupPaused(Obstacle.INTERACTIVE_CHALLENGE, "captcha"))
    tool._build_signup_flow = _flow

    async def _esc(paused, user_id, session_id, driver=None):
        return EscalationOutcome.PAUSED
    tool._escalate = _esc

    res = await tool.x_signup_start(XSignupStartAction(), execution_context=_Ctx())
    assert res.error is None
    assert "paused" in res.extracted_content.lower() or "owner" in res.extracted_content.lower()


def test_x_login_check_declares_an_explicit_param_model():
    """106 'missing param_model - auto-generating' WARNINGs/24h on prod came from
    this one parameterless action; an explicit empty model ends the noise and
    keeps the registry's strict path."""
    from pydantic import ValidationError
    from tools.x_browser.tool import XBrowserTool, XLoginCheckAction
    assert XBrowserTool.x_login_check._param_model is XLoginCheckAction
    with pytest.raises(ValidationError):
        XLoginCheckAction(extra="nope")


# --- x_reply (2026-09-19): the public-reply lane on the browser rail ---------
#
# The X API tier returns 403 on a reply to anyone who has not mentioned us, so
# the whole Phase-1 "value-first replies" lane of the outreach programme was
# dead — and the browser rail, built to bypass exactly that tier, had no reply
# verb. `x_reply` opens the target status in the saved session and posts under
# it. Same gates as `x_post`: owner-approval (DEFAULT_APPROVAL_REQUIRED_TOOLS),
# hourly write cap, leaf/forged turns refused, structured no-session error.

def test_x_reply_is_approval_gated():
    from tools.controller.approval import DEFAULT_APPROVAL_REQUIRED_TOOLS
    assert "x_browser_x_reply" in DEFAULT_APPROVAL_REQUIRED_TOOLS


def test_x_reply_action_accepts_url_or_id_and_caps_length():
    from pydantic import ValidationError
    from tools.x_browser.tool import XReplyAction
    a = XReplyAction(in_reply_to="https://x.com/someone/status/1234567890", text="hi")
    assert a.status_id == "1234567890"
    b = XReplyAction(in_reply_to="1234567890", text="hi")
    assert b.status_id == "1234567890"
    with pytest.raises(ValidationError):
        XReplyAction(in_reply_to="https://x.com/someone", text="hi")
    with pytest.raises(ValidationError):
        XReplyAction(in_reply_to="1234567890", text="x" * 281)


@pytest.mark.asyncio
async def test_x_reply_no_session_is_structured_error(monkeypatch):
    from tools.x_browser.tool import XReplyAction
    tool = _tool(monkeypatch, session=None)
    res = await tool.x_reply(XReplyAction(in_reply_to="1234567890", text="hi"),
                             execution_context=_Ctx())
    assert res.error and "no x session" in res.error.lower()


@pytest.mark.asyncio
async def test_x_reply_success_returns_url(monkeypatch):
    from tools.x_browser.tool import XReplyAction

    class _ReplyDriver(FakeDriver):
        def __init__(self):
            super().__init__(url="https://x.com/robbot/status/77")
            self.replied = None

        async def reply(self, status_id, text):
            self.replied = (status_id, text)
            return self._url

    driver = _ReplyDriver()
    tool = _tool(monkeypatch, session={"handle": "robbot", "storage_state": {}},
                 driver=driver)
    res = await tool.x_reply(
        XReplyAction(in_reply_to="https://x.com/alice/status/1234567890", text="good point"),
        execution_context=_Ctx())
    assert res.error is None
    assert driver.replied == ("1234567890", "good point")
    assert "https://x.com/robbot/status/77" in res.extracted_content


@pytest.mark.asyncio
async def test_x_reply_refused_for_leaf(monkeypatch):
    from tools.x_browser.tool import XReplyAction
    tool = _tool(monkeypatch, session={"handle": "robbot", "storage_state": {}},
                 driver=FakeDriver())
    res = await tool.x_reply(XReplyAction(in_reply_to="1234567890", text="hi"),
                             execution_context=_Ctx(role="leaf"))
    assert res.error and "blocked" in res.error.lower()
