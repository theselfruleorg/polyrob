"""A3 (2026-07-13 correspondent review): real email threading.

Outbound email never minted/stored its own Message-ID and set no In-Reply-To/
References headers, so the correspondent registry's thread-disambiguation was
dead code (routing degraded to address-only). Now:
- `send_email_ex` mints a Message-ID (domain from the From address), sets
  optional In-Reply-To/References, and returns the Message-ID on success;
- `send_email` keeps its bool contract by delegating.
"""
import pytest

from tools.email_tool import EmailTool


class _FakeSMTP:
    def __init__(self):
        self.messages = []

    def send_message(self, msg):
        self.messages.append(msg)


def _tool():
    tool = object.__new__(EmailTool)
    tool.config = type("C", (), {"gmail_email": "rob@selfrule.org"})()
    tool._enabled = True
    tool.smtp_connection = _FakeSMTP()
    import logging
    tool.logger = logging.getLogger("test-email-tool")

    async def _noop():
        return None
    tool.ensure_initialized = _noop
    return tool


@pytest.mark.asyncio
async def test_send_email_ex_mints_and_returns_message_id():
    tool = _tool()
    mid = await tool.send_email_ex("john@acme.com", "Hello", "body text")
    assert mid and mid.startswith("<") and mid.endswith(">")
    assert "selfrule.org" in mid
    sent = tool.smtp_connection.messages[0]
    assert sent["Message-ID"] == mid
    assert sent["To"] == "john@acme.com"


@pytest.mark.asyncio
async def test_send_email_ex_sets_threading_headers():
    tool = _tool()
    await tool.send_email_ex(
        "john@acme.com", "Re: Hello", "reply body",
        in_reply_to="<orig-123@acme.com>",
        references="<root-1@acme.com> <orig-123@acme.com>")
    sent = tool.smtp_connection.messages[0]
    assert sent["In-Reply-To"] == "<orig-123@acme.com>"
    assert sent["References"] == "<root-1@acme.com> <orig-123@acme.com>"


@pytest.mark.asyncio
async def test_in_reply_to_alone_becomes_references():
    tool = _tool()
    await tool.send_email_ex("john@acme.com", "Re: Hello", "reply",
                             in_reply_to="<orig-123@acme.com>")
    sent = tool.smtp_connection.messages[0]
    assert sent["References"] == "<orig-123@acme.com>"


@pytest.mark.asyncio
async def test_send_email_bool_contract_preserved():
    tool = _tool()
    ok = await tool.send_email("john@acme.com", "Hello", "body")
    assert ok is True
    assert len(tool.smtp_connection.messages) == 1
    assert tool.smtp_connection.messages[0]["Message-ID"]
