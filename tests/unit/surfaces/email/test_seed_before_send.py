"""A5/E5 (2026-07-13 correspondent review): seed BEFORE send + owner visibility.

- A5: the reply binding is attempted BEFORE the email leaves. A cap-refused seed
  BLOCKS the send (previously the email still went out and the reply was
  orphaned — unroutable forever, invisibly).
- E5: a NEW pending seed is loud — WARNING log + a `correspondent_pending`
  telemetry event — and `polyrob owner pending` lists pending correspondents
  (previously only `owner correspondents` showed them, with no notification).
"""
import os
import tempfile

import pytest

from core.surfaces.correspondents import CorrespondentRegistry
from core.surfaces.envelopes import OutboundMessage
from surfaces.email.surface import EmailSurface


class _Sender:
    def __init__(self):
        self.sent = []

    async def send_email(self, to_email, subject, body, **kw):
        self.sent.append(to_email)
        return True


class _ChatReg:
    def resolve(self, session_key):
        return {"session_id": "orig_sess", "user_id": "u_owner"}


class _Container:
    def __init__(self, corr):
        self._svc = {"correspondent_registry": corr, "session_chat_registry": _ChatReg()}

    def get_service(self, name):
        return self._svc.get(name)


def _registry():
    return CorrespondentRegistry(os.path.join(tempfile.mkdtemp(), "corr.db"))


@pytest.mark.asyncio
async def test_refused_seed_blocks_send(monkeypatch):
    """Cap reached -> binding refused -> the email must NOT be sent."""
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_MAX_NEW_PER_DAY", "0")
    sender = _Sender()
    surface = EmailSurface(sender)
    await surface.start(_Container(_registry()))
    res = await surface.send(OutboundMessage(
        session_key="agent:main:email:dm:john@acme.com", text="hello"))
    assert res.success is False
    assert "cap" in (res.error or "").lower()
    assert sender.sent == [], "a cap-refused binding must block the send"


@pytest.mark.asyncio
async def test_existing_correspondent_still_sends(monkeypatch):
    """An already-bound address is not 'new' — cap can't block replying to them."""
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_MAX_NEW_PER_DAY", "0")
    corr = _registry()
    corr.seed(surface="email", address="john@acme.com", session_id="orig_sess",
              user_id="u_owner", require_approval=False)
    sender = _Sender()
    surface = EmailSurface(sender)
    await surface.start(_Container(corr))
    res = await surface.send(OutboundMessage(
        session_key="agent:main:email:dm:john@acme.com", text="hello again"))
    assert res.success is True
    assert sender.sent == ["john@acme.com"]


@pytest.mark.asyncio
async def test_disabled_model_still_sends(monkeypatch):
    """Access model off -> legacy behavior: send proceeds, no binding."""
    monkeypatch.delenv("CORRESPONDENT_ACCESS_ENABLED", raising=False)
    corr = _registry()
    sender = _Sender()
    surface = EmailSurface(sender)
    await surface.start(_Container(corr))
    res = await surface.send(OutboundMessage(
        session_key="agent:main:email:dm:john@acme.com", text="hi"))
    assert res.success is True
    assert sender.sent == ["john@acme.com"]
    assert corr.resolve(surface="email", address="john@acme.com") is None


def test_new_pending_seed_emits_event(monkeypatch):
    """E5: a NEW pending correspondent emits a correspondent_pending event."""
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "true")
    monkeypatch.delenv("CORRESPONDENT_MAX_NEW_PER_DAY", raising=False)

    recorded = []

    class _EL:
        def record(self, kind, **kw):
            recorded.append((kind, kw))

    import core.surfaces.seed as seed_mod
    monkeypatch.setattr(seed_mod, "_event_log", lambda: _EL())

    corr = _registry()

    class _C:
        def get_service(self, name):
            return corr if name == "correspondent_registry" else None

    state = seed_mod.maybe_seed_correspondent(
        _C(), surface="email", address="new@acme.com",
        session_id="s1", user_id="u_owner")
    assert state == "pending"
    assert any(k == "correspondent_pending" for k, _ in recorded)

    # Re-seeding the SAME address is idempotent — no duplicate event.
    recorded.clear()
    seed_mod.maybe_seed_correspondent(
        _C(), surface="email", address="new@acme.com",
        session_id="s1", user_id="u_owner")
    assert recorded == []


def test_registry_exists():
    corr = _registry()
    assert corr.exists(surface="email", address="a@b.com", user_id="u1") is False
    corr.seed(surface="email", address="a@b.com", session_id="s", user_id="u1")
    assert corr.exists(surface="email", address="A@B.com", user_id="u1") is True
    assert corr.exists(surface="email", address="a@b.com", user_id="u2") is False


def test_owner_pending_includes_correspondents():
    """The pure helper behind `polyrob owner pending` lists pending correspondents."""
    from cli.commands.owner import _pending_correspondent_items
    corr = _registry()
    corr.seed(surface="email", address="p@x.com", session_id="s1", user_id="t1",
              require_approval=True)
    corr.seed(surface="email", address="a@x.com", session_id="s2", user_id="t1",
              require_approval=False)  # active — must not be listed
    items = _pending_correspondent_items(corr, "t1")
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "correspondent"
    assert "p@x.com" in it["preview"]
