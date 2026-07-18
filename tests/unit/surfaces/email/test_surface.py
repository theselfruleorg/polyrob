"""WS-B EmailSurface — outbound over SMTP, buffered (no live edit)."""
import pytest

from core.surfaces.envelopes import MessageKind, OutboundMessage, SurfaceCapabilities
from surfaces.email.surface import EmailSurface, address_from_session_key


class _Sender:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []
        self.kwargs = []

    async def send_email(self, to_email, subject, body, **kw):
        self.sent.append((to_email, subject, body))
        self.kwargs.append(kw)
        return self.ok


def test_address_from_session_key():
    assert address_from_session_key("agent:main:email:dm:john@acme.com") == "john@acme.com"
    assert address_from_session_key("agent:main:email:dm:john@acme.com:u_x") == "john@acme.com"


def test_capabilities_no_live_edit():
    cap = EmailSurface(_Sender()).capabilities
    assert isinstance(cap, SurfaceCapabilities)
    assert cap.supports_streaming is True       # buffered one-message-per-turn
    assert cap.supports_edit is False           # email has no in-place edit
    assert cap.supports_interactive_ask is False


def test_surface_id():
    assert EmailSurface(_Sender()).surface_id == "email"


@pytest.mark.asyncio
async def test_send_delivers_body_to_resolved_address():
    s = _Sender()
    surface = EmailSurface(s)
    msg = OutboundMessage(session_key="agent:main:email:dm:john@acme.com",
                          text="Thanks — received.", kind=MessageKind.AGENT_TEXT)
    res = await surface.send(msg)
    assert res.success is True
    assert s.sent and s.sent[0][0] == "john@acme.com"
    assert s.sent[0][2] == "Thanks — received."


@pytest.mark.asyncio
async def test_send_fail_open_on_sender_error():
    class _Boom:
        async def send_email(self, *a, **k):
            raise RuntimeError("smtp down")

    surface = EmailSurface(_Boom())
    res = await surface.send(OutboundMessage(session_key="agent:main:email:dm:x@y.com",
                                             text="hi"))
    assert res.success is False
    assert "smtp down" in (res.error or "")


def test_capabilities_media_out():
    assert EmailSurface(_Sender()).capabilities.media_out is True


@pytest.mark.asyncio
async def test_send_maps_path_media_to_attachments():
    s = _Sender()
    surface = EmailSurface(s)
    msg = OutboundMessage(
        session_key="agent:main:email:dm:john@acme.com", text="see attached",
        media=[{"kind": "image", "path": "/tmp/card.png", "caption": None}],
    )
    res = await surface.send(msg)
    assert res.success is True
    assert s.kwargs[0]["attachments"] == ["/tmp/card.png"]


@pytest.mark.asyncio
async def test_send_legacy_subject_entry_stays_subject_not_attachment():
    s = _Sender()
    surface = EmailSurface(s)
    msg = OutboundMessage(
        session_key="agent:main:email:dm:john@acme.com", text="body",
        media=[{"subject": "Custom Subject"}],
    )
    res = await surface.send(msg)
    assert res.success is True
    assert s.sent[0][1] == "Custom Subject"  # subject still extracted
    assert s.kwargs[0]["attachments"] is None  # NOT treated as an attachment


@pytest.mark.asyncio
async def test_send_no_media_passes_no_attachments():
    s = _Sender()
    surface = EmailSurface(s)
    await surface.send(OutboundMessage(session_key="agent:main:email:dm:x@y.com", text="hi"))
    assert s.kwargs[0]["attachments"] is None
