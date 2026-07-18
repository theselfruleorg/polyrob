"""A1/A2 (2026-07-13 correspondent review): the proactive `message` tool must
create a reply binding on EVERY surface.

Previously it routed through router.send_message with a synthetic
`direct:{surface}:{chat}` session key that was never in session_chat_map, so
email's surface-level seed short-circuited and no other surface seeded at all —
an agent-initiated first contact got its reply DENIED at the routing boundary.

Now perform_message_send seeds the correspondent registry itself (BEFORE the
send, parity with A5): non-owner targets get a (surface, address) -> session
binding; owner targets are never seeded; a cap-refused binding blocks the send.
"""
import asyncio
import os
import tempfile

from core.surfaces.correspondents import CorrespondentRegistry
from core.surfaces.outbound_allowlist import OutboundAllowlist
from tools.controller.message_send import perform_message_send


class _Router:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((surface_id, chat_id, text))
        return True

    def capabilities(self, surface_id):
        return None


class _Container:
    def __init__(self, corr):
        self._corr = corr

    def get_service(self, name):
        return self._corr if name == "correspondent_registry" else None


def _fixtures():
    tmp = tempfile.mkdtemp()
    corr = CorrespondentRegistry(os.path.join(tmp, "corr.db"))
    allowlist = OutboundAllowlist(os.path.join(tmp, "a.db"))
    return corr, allowlist


def test_allowlisted_send_seeds_reply_binding(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")
    corr, allowlist = _fixtures()
    allowlist.allow("rob", "telegram", "555")
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="555", text="hi",
        session_id="sess-1", container=_Container(corr)))
    assert res["success"] is True
    row = corr.resolve(surface="telegram", address="555")
    assert row is not None and row["session_id"] == "sess-1"


def test_owner_send_never_seeds(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    corr, allowlist = _fixtures()
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi",
        session_id="sess-1", container=_Container(corr)))
    assert res["success"] is True
    assert corr.list() == [], "the owner is not a correspondent"


def test_cap_refused_binding_blocks_proactive_send(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_MAX_NEW_PER_DAY", "0")
    corr, allowlist = _fixtures()
    allowlist.allow("rob", "email", "new@acme.com")
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={},
        user_id="rob", surface="email", target="new@acme.com", text="hi",
        session_id="sess-1", container=_Container(corr)))
    assert res["success"] is False
    assert "cap" in (res.get("error") or "").lower()
    assert router.sent == []


def test_pending_seed_sends_with_note(monkeypatch):
    """Approval-gated seed -> message still goes out, result carries the pending note."""
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "true")
    corr, allowlist = _fixtures()
    allowlist.allow("rob", "email", "new@acme.com")
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={},
        user_id="rob", surface="email", target="new@acme.com", text="hi",
        session_id="sess-1", container=_Container(corr)))
    assert res["success"] is True
    assert router.sent, "pending approval gates the REPLY, not the outbound send"
    assert res.get("correspondent") == "pending"
    assert "pending" in (res.get("note") or "").lower()


def test_no_container_is_fail_soft(monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    _, allowlist = _fixtures()
    allowlist.allow("rob", "telegram", "555")
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={},
        user_id="rob", surface="telegram", target="555", text="hi",
        session_id="sess-1"))
    assert res["success"] is True


def test_generic_seed_module_exists():
    """core.surfaces.seed is the generic home; the email module stays a shim."""
    from core.surfaces.seed import maybe_seed_correspondent as core_seed
    from core.surfaces.seed import maybe_seed_correspondent as email_seed
    assert core_seed is email_seed
