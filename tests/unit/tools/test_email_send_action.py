"""EmailTool.email_send — the agent-callable send action (proposal 011 / inbox item
#1: EmailTool previously exposed ZERO actions, so a goal session that loaded
`tool_ids=['email']` had nothing to call). Gated the same way as the generic
`message` action: owner/allowlisted targets only, correspondent seed-before-send.
No network: smtp_connection is a fake that just records the built message."""
import os
import tempfile
import types

import pytest

from core.surfaces.conversations import ConversationStore
from core.surfaces.correspondents import CorrespondentRegistry
from core.surfaces.outbound_allowlist import OutboundAllowlist
from tools.email_tool import EmailSendAction, EmailTool


def _tool(container=None):
    cfg = types.SimpleNamespace(
        gmail_email="bot@example.com",
        gmail_app_password="app-pw",
        gmail_smtp_server="smtp.test",
        gmail_smtp_port=587,
        gmail_imap_server="imap.test",
    )
    tool = EmailTool(name="email", config=cfg, container=None)
    tool._initialized = True  # skip ensure_initialized()'s real SMTP handshake
    tool._enabled = True
    if container is not None:
        tool.container = container  # bypass the lazy DependencyContainer singleton
    return tool


class _FakeSMTP:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)


class _FakeContainer:
    def __init__(self, allowlist=None, convo=None, corr=None):
        self._allowlist = allowlist
        self._convo = convo
        self._corr = corr

    def get_service(self, name):
        if name == "outbound_allowlist":
            return self._allowlist
        if name == "conversation_store":
            return self._convo
        if name == "correspondent_registry":
            return self._corr
        return None  # absent is fine, fail-soft


def _al():
    tmp = tempfile.mkdtemp()
    return OutboundAllowlist(os.path.join(tmp, "a.db"))


def _convo():
    tmp = tempfile.mkdtemp()
    return ConversationStore(os.path.join(tmp, "conversations.db"))


def _corr():
    tmp = tempfile.mkdtemp()
    return CorrespondentRegistry(os.path.join(tmp, "corr.db"))


class _Ctx:
    def __init__(self, user_id="rob", session_id="s1"):
        self.user_id = user_id
        self.session_id = session_id


def test_email_send_registered_as_an_action():
    # get_actions() enumerates every attribute (incl. the `container` property) —
    # give it a fake container so it doesn't fall through to the real, uninitialized
    # DependencyContainer singleton in an isolated test process.
    tool = _tool(container=_FakeContainer())
    actions = tool.get_actions()
    assert "email_send" in actions


@pytest.mark.asyncio
async def test_denied_target_is_not_sent(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("BOT_OWNER_EMAIL", raising=False)
    tool = _tool(container=_FakeContainer(allowlist=_al()))
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp

    result = await tool.email_send(
        EmailSendAction(to="stranger@example.com", subject="Hi", body="body"),
        execution_context=_Ctx(),
    )

    assert smtp.sent == []
    assert result.error is not None and "allowlist" in result.error


@pytest.mark.asyncio
async def test_owner_target_is_sent(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    tool = _tool(container=_FakeContainer(allowlist=_al()))
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp

    result = await tool.email_send(
        EmailSendAction(to="owner@example.com", subject="Hi", body="body"),
        execution_context=_Ctx(),
    )

    assert len(smtp.sent) == 1
    assert smtp.sent[0]["To"] == "owner@example.com"
    assert result.error is None
    assert "OK" in result.extracted_content


@pytest.mark.asyncio
async def test_allowlisted_target_is_sent(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("BOT_OWNER_EMAIL", raising=False)
    allowlist = _al()
    allowlist.allow("rob", "email", "partner@example.com", note="battle-test")
    tool = _tool(container=_FakeContainer(allowlist=allowlist))
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp

    result = await tool.email_send(
        EmailSendAction(to="partner@example.com", subject="Hi", body="body"),
        execution_context=_Ctx(),
    )

    assert len(smtp.sent) == 1
    assert result.error is None


# --- 013 T6: outbound policy enforcement (open-tier cap + first-contact report) --

@pytest.mark.asyncio
async def test_open_policy_unknown_recipient_sends_and_reports(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("BOT_OWNER_EMAIL", raising=False)
    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")

    notified = []

    async def _fake_deliver(container, user_id, text, **kw):
        notified.append((user_id, text, kw.get("source")))
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _fake_deliver)

    convo = _convo()
    corr = _corr()
    tool = _tool(container=_FakeContainer(allowlist=_al(), convo=convo, corr=corr))
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp

    result = await tool.email_send(
        EmailSendAction(to="stranger@example.com", subject="Hi", body="body"),
        execution_context=_Ctx(),
    )

    assert len(smtp.sent) == 1
    assert result.error is None
    assert notified and notified[0][2] == "outbound_open_send"
    assert convo.get("rob", "email", "stranger@example.com") is not None
    assert corr.resolve(surface="email", address="stranger@example.com") is not None, \
        "correspondent must be seeded on a first-contact open send"


@pytest.mark.asyncio
async def test_open_policy_daily_cap_blocks_email(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("BOT_OWNER_EMAIL", raising=False)
    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    monkeypatch.setenv("OUTBOUND_DAILY_SEND_CAP", "1")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")

    convo = _convo()
    convo.record_outbound("rob", "email", "already@x.com", "prior")
    tool = _tool(container=_FakeContainer(allowlist=_al(), convo=convo))
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp

    result = await tool.email_send(
        EmailSendAction(to="stranger@example.com", subject="Hi", body="body"),
        execution_context=_Ctx(),
    )

    assert smtp.sent == []
    assert result.error is not None
    assert "outbound.daily_send_cap" in result.error


@pytest.mark.asyncio
async def test_open_policy_seed_refused_blocks_email(monkeypatch):
    monkeypatch.delenv("POLYROB_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("BOT_OWNER_EMAIL", raising=False)
    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_MAX_NEW_PER_DAY", "0")

    tool = _tool(container=_FakeContainer(allowlist=_al(), convo=_convo(), corr=_corr()))
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp

    result = await tool.email_send(
        EmailSendAction(to="stranger@example.com", subject="Hi", body="body"),
        execution_context=_Ctx(),
    )

    assert smtp.sent == []
    assert result.error is not None and "cap" in result.error.lower()


@pytest.mark.asyncio
async def test_denied_target_text_unchanged_under_allowlist_policy(monkeypatch):
    """Regression: OUTBOUND_POLICY unset (supervised default) keeps the exact
    legacy denied-tier error text byte-identical."""
    monkeypatch.delenv("POLYROB_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("BOT_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    tool = _tool(container=_FakeContainer(allowlist=_al()))
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp

    result = await tool.email_send(
        EmailSendAction(to="stranger@example.com", subject="Hi", body="body"),
        execution_context=_Ctx(),
    )

    assert smtp.sent == []
    assert result.error == (
        "target not on owner allowlist; ask the owner to run "
        "`polyrob owner allow email stranger@example.com`")
