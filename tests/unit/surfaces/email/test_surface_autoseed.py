"""WS-B: EmailSurface auto-seeds a correspondent on outbound to a new address.

When the agent emails out through the surface, the recipient becomes a (guard-railed)
correspondent so their reply can later route back as DATA. Safe: approval-gated, and a
seed fault never breaks the send.
"""
import os
import tempfile

import pytest

from core.surfaces.correspondents import CorrespondentRegistry
from core.surfaces.envelopes import OutboundMessage
from surfaces.email.surface import EmailSurface


class _Sender:
    async def send_email(self, to_email, subject, body, **kw):
        return True


class _ChatReg:
    def resolve(self, session_key):
        return {"session_id": "orig_sess", "user_id": "u_owner"}


class _Container:
    def __init__(self, corr):
        self._svc = {"correspondent_registry": corr, "session_chat_registry": _ChatReg()}

    def get_service(self, name):
        return self._svc.get(name)


@pytest.mark.asyncio
async def test_send_autoseeds_recipient(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")  # -> active for the assert
    corr = CorrespondentRegistry(os.path.join(tempfile.mkdtemp(), "corr.db"))
    surface = EmailSurface(_Sender())
    await surface.start(_Container(corr))
    msg = OutboundMessage(session_key="agent:main:email:dm:john@acme.com", text="hello")
    res = await surface.send(msg)
    assert res.success is True
    row = corr.resolve(surface="email", address="john@acme.com")
    assert row is not None and row["session_id"] == "orig_sess"


@pytest.mark.asyncio
async def test_send_without_container_still_succeeds(monkeypatch):
    """No bus/container wired -> send still works, no seed, no crash."""
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    surface = EmailSurface(_Sender())  # start() never called
    res = await surface.send(OutboundMessage(session_key="agent:main:email:dm:x@y.com",
                                             text="hi"))
    assert res.success is True


@pytest.mark.asyncio
async def test_no_seed_when_flag_off(monkeypatch):
    monkeypatch.delenv("CORRESPONDENT_ACCESS_ENABLED", raising=False)
    corr = CorrespondentRegistry(os.path.join(tempfile.mkdtemp(), "corr.db"))
    surface = EmailSurface(_Sender())
    await surface.start(_Container(corr))
    await surface.send(OutboundMessage(session_key="agent:main:email:dm:john@acme.com",
                                       text="hi"))
    assert corr.resolve(surface="email", address="john@acme.com") is None
