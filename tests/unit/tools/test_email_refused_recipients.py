"""SMTP refused recipients are not "sent".

``conn.send_message`` returns the recipients the server refused (when at least
one was accepted). ``_send_marking_data`` dropped that dict, so the action said
"OK" for every address, recorded the refused ones in the transcript and bound
thread anchors for mail that never left.
"""
import types

import pytest

from tools.email_tool import EmailSendAction, EmailTool, SentMessageId

REFUSED = {"gone@example.test": (550, b"5.1.1 no such user")}


def _tool(container=None):
    cfg = types.SimpleNamespace(
        gmail_email="agent@example.test", gmail_app_password="test-only",
        gmail_smtp_server="smtp.test", gmail_smtp_port=587, gmail_imap_server="imap.test")
    tool = EmailTool(name="email", config=cfg, container=container)
    tool.container = container
    tool._initialized = True
    tool._enabled = True
    return tool


@pytest.mark.asyncio
async def test_send_email_ex_carries_the_refused_recipients():
    class _Conn:
        def send_message(self, msg):
            return dict(REFUSED)
    tool = _tool()
    tool.smtp_connection = _Conn()
    mid = await tool.send_email_ex(["ok@example.test", "gone@example.test"], "s", "b")
    assert isinstance(mid, str) and mid.startswith("<")
    assert mid.refused == REFUSED


class _Store:
    def __init__(self):
        self.recorded = []

    def record_outbound(self, user_id, surface, addr, body, **kw):
        self.recorded.append(addr)

    def get(self, *a, **kw):
        return object()

    def outbound_count_since(self, *a, **kw):
        return 0

    def outbound_count_surface_since(self, *a, **kw):
        return 0


class _Container:
    def __init__(self, store):
        self.store = store

    def get_service(self, name):
        return self.store if name == "conversation_store" else None


@pytest.mark.asyncio
async def test_gated_send_names_refused_addresses_and_records_only_the_delivered(monkeypatch):
    monkeypatch.setattr("core.surfaces.outbound_target.resolve_target_tier",
                        lambda **kw: "allowlisted")
    monkeypatch.setattr("core.surfaces.seed.maybe_seed_correspondent",
                        lambda *a, **kw: "seeded")
    store = _Store()
    tool = _tool(_Container(store))
    anchored = []
    monkeypatch.setattr(tool, "_seed_thread_anchor",
                        lambda addr, mid, uid, sid: anchored.append(addr))

    async def _send(*a, **kw):
        return SentMessageId("<m1@example.test>", REFUSED)
    monkeypatch.setattr(tool, "send_email_ex", _send)

    ctx = types.SimpleNamespace(user_id="u1", session_id="s1", is_sub_agent=False,
                                role="orchestrator", metadata={})
    res = await tool.email_send(
        EmailSendAction(to="ok@example.test, gone@example.test", subject="s", body="b"),
        execution_context=ctx)
    assert res.error is None
    assert "NOT delivered to: gone@example.test" in res.extracted_content
    assert "-> ok@example.test OK" in res.extracted_content
    assert store.recorded == ["ok@example.test"]
    assert anchored == ["ok@example.test"]
