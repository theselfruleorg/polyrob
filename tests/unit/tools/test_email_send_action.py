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


# --- D9 / D19 / D31 / D66 (2026-09-21 interface audit) ----------------------

class _ForgedCtx(_Ctx):
    """An autonomous/forged turn — what a goal or cron run looks like."""

    def __init__(self):
        super().__init__()
        self.is_sub_agent = False
        self.role = "orchestrator"
        self.metadata = {"turn_kind": "self_wake"}


@pytest.mark.asyncio
async def test_a_paused_owner_blocks_an_autonomous_email(monkeypatch, tmp_path):
    """D9: `email_send` had NO pause probe, so `/pause pings`, `/pause social`
    and even `/pause all` left this escape hatch wide open for a goal or cron
    session — the dominant autonomous outbound path."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    import core.autonomy_control as ac
    ac.pause(str(tmp_path), scopes=("all",))
    try:
        tool = _tool(container=_FakeContainer(convo=_convo(), corr=_corr()))
        smtp = _FakeSMTP()
        tool.smtp_connection = smtp
        res = await tool.email_send(
            EmailSendAction(to="owner@example.com", subject="s", body="b"),
            execution_context=_ForgedCtx())
        assert res.error and "paused" in res.error.lower()
        assert smtp.sent == []          # nothing left the box
    finally:
        ac.resume(str(tmp_path))


@pytest.mark.asyncio
async def test_a_secret_shape_is_scrubbed_before_the_mail_leaves(monkeypatch):
    """D66: this tool owns its own SMTP connection and bypassed
    `MessageRouter.publish`, so it was the ONE outbound path that could mail a
    key out verbatim."""
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    convo = _convo()
    tool = _tool(container=_FakeContainer(convo=convo, corr=_corr()))
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp
    secret = "sk-ant-api03-" + "C" * 40
    res = await tool.email_send(
        EmailSendAction(to="owner@example.com", subject="keys",
                        body=f"here it is: {secret}"),
        execution_context=_Ctx())
    assert res.error is None
    body = smtp.sent[0].get_payload()
    assert secret not in str(body)


@pytest.mark.asyncio
async def test_the_scrubbed_body_is_the_one_string_recorded(monkeypatch):
    """The cooldown gate hashes the body the store records; two spellings make
    the hashes unmatchable, which kills the gate while it still looks present."""
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    convo = _convo()
    tool = _tool(container=_FakeContainer(convo=convo, corr=_corr()))
    tool.smtp_connection = _FakeSMTP()
    secret = "sk-ant-api03-" + "D" * 40
    await tool.email_send(
        EmailSendAction(to="owner@example.com", subject="k", body=f"x {secret}"),
        execution_context=_Ctx())
    rows = convo.history("rob", "email", "owner@example.com")
    assert rows and secret not in rows[-1]["body"]


@pytest.mark.asyncio
async def test_a_non_owner_send_anchors_its_message_id(monkeypatch):
    """D31: the minted Message-ID was dropped, so a reply's In-Reply-To had
    nothing to resolve on and a second session talking to the same address was
    ambiguous."""
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")
    al = _al()
    al.allow("rob", "email", "them@acme.com")
    corr = _corr()
    tool = _tool(container=_FakeContainer(allowlist=al, convo=_convo(), corr=corr))
    tool.smtp_connection = _FakeSMTP()
    res = await tool.email_send(
        EmailSendAction(to="them@acme.com", subject="hello", body="the quote"),
        execution_context=_Ctx())
    assert res.error is None
    anchored = [r for r in corr.list("rob") if r.get("thread_id")]
    assert anchored, "the outbound Message-ID was never bound to the session"
