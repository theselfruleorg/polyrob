"""2026-08-29 fix: `email_send` (the dedicated-tool escape hatch used when the
generic `message` tool's `surface="email"` path is unreachable) previously had
no owner-message cooldown/dedup at all, unlike the `message` action. A
completion-judge retry sent the owner the same decision-board report twice,
3 minutes apart (observed 2026-08-28 22:19Z). This mirrors the
`message` action's cooldown gate (tests/unit/tools/controller/
test_message_owner_cooldown.py already covers the pure predicate) onto
`EmailTool.email_send`.
"""
import types

import pytest

from tools.email_tool import EmailSendAction, EmailTool


class _Ctx:
    def __init__(self, is_sub_agent=False, role="orchestrator", user_id="rob", session_id="s1"):
        self.is_sub_agent = is_sub_agent
        self.role = role
        self.user_id = user_id
        self.session_id = session_id
        self.metadata = {}


_AUTONOMOUS_CTX = _Ctx(is_sub_agent=True)
_OWNER_CTX = _Ctx(is_sub_agent=False, role="orchestrator")


class _FakeStore:
    def __init__(self, count=0):
        self._count = count
        self.calls = []
        self.recorded = []

    def outbound_count_since(self, user_id, surface, address, since_secs, **kw):
        self.calls.append((user_id, surface, address, since_secs))
        return self._count

    def get(self, *a, **kw):
        return object()  # not first-contact; irrelevant for owner-tier sends

    def record_outbound(self, *a, **kw):
        self.recorded.append((a, kw))

    def outbound_count_surface_since(self, *a, **kw):
        return 0


class _FakeContainer:
    def __init__(self, store):
        self._store = store

    def get_service(self, name):
        if name == "conversation_store":
            return self._store
        return None


def _tool(container):
    cfg = types.SimpleNamespace(
        gmail_email="bot@example.com", gmail_app_password="app-pw",
        gmail_smtp_server="smtp.test", gmail_smtp_port=587, gmail_imap_server="imap.test",
    )
    tool = EmailTool(name="email", config=cfg, container=container)
    tool.container = container  # BaseComponent.__init__ doesn't apply the ctor kwarg
    tool._initialized = True
    tool._enabled = True
    return tool


@pytest.fixture(autouse=True)
def _owner_email(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_email", lambda env: "owner@example.com")
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")


@pytest.mark.asyncio
async def test_autonomous_send_refused_within_cooldown(monkeypatch):
    store = _FakeStore(count=1)
    tool = _tool(_FakeContainer(store))
    sent = []
    monkeypatch.setattr(tool, "send_email_ex", lambda *a, **kw: sent.append(1))

    res = await tool.email_send(
        EmailSendAction(to="owner@example.com", subject="Board", body="report"),
        execution_context=_AUTONOMOUS_CTX)

    assert "contact_history" in (res.extracted_content or "")
    assert not sent  # never reached send_email_ex
    assert store.calls == [("rob", "email", "owner@example.com", 7200)]


@pytest.mark.asyncio
async def test_autonomous_send_allowed_when_no_recent_send(monkeypatch):
    store = _FakeStore(count=0)
    tool = _tool(_FakeContainer(store))

    async def _fake_send(*a, **kw):
        return "mid-123"
    monkeypatch.setattr(tool, "send_email_ex", _fake_send)

    res = await tool.email_send(
        EmailSendAction(to="owner@example.com", subject="Board", body="report"),
        execution_context=_AUTONOMOUS_CTX)

    assert "OK" in res.extracted_content


@pytest.mark.asyncio
async def test_genuine_owner_turn_never_gated_even_with_recent_send(monkeypatch):
    store = _FakeStore(count=5)
    tool = _tool(_FakeContainer(store))

    async def _fake_send(*a, **kw):
        return "mid-456"
    monkeypatch.setattr(tool, "send_email_ex", _fake_send)

    res = await tool.email_send(
        EmailSendAction(to="owner@example.com", subject="Board", body="report"),
        execution_context=_OWNER_CTX)

    assert "OK" in res.extracted_content
    assert store.calls == []  # cooldown never even queried for a genuine owner turn
