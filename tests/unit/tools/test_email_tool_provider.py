"""Provider-aware EmailTool (Task 4, 2026-08-18 plan).

provider=agentmail: no SMTP requirement, provisioning at init, HTTP send/read.
provider=smtp: byte-identical legacy behaviour.
"""
import pytest

from core.config import BotConfig
from core.exceptions import ConfigurationError
from tools.email_tool import EmailTool


class FakeAgentMail:
    def __init__(self):
        self.provisioned = False
        self.sent = []
        self.address = "rob@agentmail.to"

    async def provision(self, instance_id, username=None):
        self.provisioned = True
        return {"inbox_id": "in_1", "address": self.address}

    async def send(self, to, subject, text, **kwargs):
        self.sent.append({"to": to, "subject": subject, "text": text, **kwargs})
        return "<minted@agentmail.to>"

    async def list_messages(self, limit=25):
        return [{"message_id": "m1", "from": "a@b.c", "subject": "s",
                 "timestamp": "2026-08-18T00:00:00Z"}]

    async def get_message(self, message_id):
        return {"message_id": message_id, "from": "a@b.c", "subject": "s",
                "timestamp": "2026-08-18T00:00:00Z",
                "text": "full text", "extracted_text": "reply only",
                "html": "<p>full text</p>"}

    async def aclose(self):
        pass


def _tool(monkeypatch, provider, **cfg):
    if provider == "agentmail":
        monkeypatch.setenv("AGENTMAIL_API_KEY", "am_test")
        monkeypatch.setenv("EMAIL_PROVIDER", "agentmail")
    else:
        monkeypatch.delenv("AGENTMAIL_API_KEY", raising=False)
        monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    config = BotConfig(**cfg)
    tool = EmailTool("email", config, None)
    if provider == "agentmail":
        tool.agentmail = FakeAgentMail()
    return tool


@pytest.mark.asyncio
async def test_agentmail_initialize_provisions_without_smtp(monkeypatch):
    tool = _tool(monkeypatch, "agentmail")  # no gmail creds at all
    await tool._initialize()
    assert tool.agentmail.provisioned is True


@pytest.mark.asyncio
async def test_smtp_without_creds_still_raises(monkeypatch):
    tool = _tool(monkeypatch, "smtp")
    with pytest.raises(Exception):
        await tool._initialize()


@pytest.mark.asyncio
async def test_send_email_ex_routes_to_agentmail(monkeypatch):
    tool = _tool(monkeypatch, "agentmail")
    tool._initialized = True
    tool._enabled = True
    mid = await tool.send_email_ex("to@example.com", "Sub", "Body",
                                   in_reply_to="<x@y.z>")
    assert mid == "<minted@agentmail.to>"
    assert tool.agentmail.sent[0]["to"] == "to@example.com"
    assert tool.agentmail.sent[0]["in_reply_to"] == "<x@y.z>"


@pytest.mark.asyncio
async def test_read_emails_maps_provider_shape(monkeypatch):
    tool = _tool(monkeypatch, "agentmail")
    tool._initialized = True
    tool._enabled = True
    emails = await tool.read_emails(limit=5)
    assert emails[0]["id"] == "m1"
    assert emails[0]["subject"] == "s"
    assert emails[0]["from"] == "a@b.c"
    assert emails[0]["content"] == "reply only"  # extracted_text preferred
    assert emails[0]["html_content"] == "<p>full text</p>"


def test_provider_attr_resolution(monkeypatch):
    assert _tool(monkeypatch, "agentmail").provider == "agentmail"
    assert _tool(monkeypatch, "smtp").provider == "smtp"
