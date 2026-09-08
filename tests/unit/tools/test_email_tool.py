"""EmailTool.send_email — attachment MIME structure (Task 7). No network: the tool's
_initialized flag is set directly and smtp_connection is a fake that just records the
built email.message.Message."""
import smtplib
import types

import pytest

from tools.email_tool import EmailTool


def _tool():
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
    return tool


class _FakeSMTP:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_send_email_without_attachments_keeps_flat_alternative_shape():
    tool = _tool()
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp

    ok = await tool.send_email("x@y.com", "Hi", "body text")

    assert ok is True
    msg = smtp.sent[0]
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["To"] == "x@y.com" and msg["Subject"] == "Hi"


@pytest.mark.asyncio
async def test_send_email_with_attachment_sets_content_disposition_and_bytes(tmp_path):
    tool = _tool()
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp
    f = tmp_path / "card.png"
    payload = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    f.write_bytes(payload)

    ok = await tool.send_email("payer@x.com", "Invoice", "see attached",
                                attachments=[str(f)])

    assert ok is True
    msg = smtp.sent[0]
    assert msg.get_content_type() == "multipart/mixed"
    parts = list(msg.walk())
    attachments = [p for p in parts if (p.get("Content-Disposition") or "").startswith("attachment")]
    assert len(attachments) == 1
    assert 'filename="card.png"' in attachments[0]["Content-Disposition"]
    assert attachments[0].get_payload(decode=True) == payload
    # The plain-text alternative body must still be present.
    text_parts = [p for p in parts if p.get_content_type() == "text/plain"]
    assert text_parts and "see attached" in text_parts[0].get_payload()


@pytest.mark.asyncio
async def test_send_email_skips_missing_attachment_but_still_sends(tmp_path, caplog):
    tool = _tool()
    smtp = _FakeSMTP()
    tool.smtp_connection = smtp
    missing = tmp_path / "nope.png"

    with caplog.at_level("WARNING"):
        ok = await tool.send_email("x@y.com", "Hi", "body", attachments=[str(missing)])

    assert ok is True
    msg = smtp.sent[0]
    parts = list(msg.walk())
    attachments = [p for p in parts if (p.get("Content-Disposition") or "").startswith("attachment")]
    assert attachments == []
    text_parts = [p for p in parts if p.get_content_type() == "text/plain"]
    assert text_parts and "body" in text_parts[0].get_payload()


class _DeadSMTP:
    """Simulates a connection the server has already closed (e.g. after a
    timeout) — send_message raises instead of the object being falsy, so a
    naive `if not self.smtp_connection` check never reconnects."""

    def send_message(self, msg):
        raise smtplib.SMTPServerDisconnected("please run connect() first")


@pytest.mark.asyncio
async def test_send_email_reconnects_and_retries_after_stale_connection(monkeypatch):
    tool = _tool()
    tool.smtp_connection = _DeadSMTP()
    fresh = _FakeSMTP()

    async def _fake_connect_smtp():
        tool.smtp_connection = fresh

    monkeypatch.setattr(tool, "_connect_smtp", _fake_connect_smtp)

    ok = await tool.send_email("x@y.com", "Hi", "body")

    assert ok is True
    assert len(fresh.sent) == 1
    assert fresh.sent[0]["To"] == "x@y.com"


@pytest.mark.asyncio
async def test_send_email_gives_up_if_retry_also_fails(monkeypatch):
    """The retry is exactly one attempt — a persistently broken server still
    surfaces a real error rather than looping or silently dropping the send."""
    tool = _tool()
    tool.smtp_connection = _DeadSMTP()

    async def _fake_connect_smtp():
        tool.smtp_connection = _DeadSMTP()

    monkeypatch.setattr(tool, "_connect_smtp", _fake_connect_smtp)

    from core.exceptions import APIError

    with pytest.raises(APIError):
        await tool.send_email("x@y.com", "Hi", "body")
