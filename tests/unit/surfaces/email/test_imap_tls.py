"""Every IMAP entry point must verify certificates before sending credentials."""
import ssl
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tools.email_tool import EmailTool
from surfaces.email.probe import probe


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["poller", "mailbox", "probe"])
async def test_imap_uses_verifying_context(monkeypatch, entry):
    import imaplib
    seen = []
    def connect(host, **kwargs):
        context = kwargs["ssl_context"]
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        seen.append(host)
        return MagicMock()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", connect)
    if entry == "probe":
        await probe({"EMAIL_PROVIDER": "smtp", "GMAIL_EMAIL": "user@example.test",
                     "GMAIL_APP_PASSWORD": "test"})
    else:
        tool = object.__new__(EmailTool)
        tool.config = SimpleNamespace(gmail_email="user@example.test", gmail_app_password="test")
        tool.imap_server = "imap.example.test"
        if entry == "mailbox":
            tool._mbox_open()
        else:
            await tool._connect_imap()
    assert len(seen) == 1
