"""WS-A: route_inbound honours the three-tier access model when enabled.

- Flag OFF (default) -> byte-identical to legacy routing.
- Flag ON:
  - OWNER -> normal COMMAND/STEER/TASK_AGENT flow;
  - CORRESPONDENT -> RouteKind.CORRESPONDENT_DATA carrying the ORIGINATING session_id;
    a correspondent can NEVER reach COMMAND/STEER/TASK_AGENT (closed tier table);
  - unknown non-owner -> DENIED.
"""
import types

import pytest

from core.surfaces.correspondents import CorrespondentRegistry
from core.surfaces.dispatcher import route_inbound, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.session_chat_registry import SessionChatRegistry


class _Container:
    def __init__(self, tmp_path, *, chat_reg=None, corr_reg=None, dead_targets=None):
        self._svc = {}
        if chat_reg is not None:
            self._svc["session_chat_registry"] = chat_reg
        if corr_reg is not None:
            self._svc["correspondent_registry"] = corr_reg
        if dead_targets is not None:
            self._svc["dead_targets"] = dead_targets
        self.config = types.SimpleNamespace(data_dir=str(tmp_path))

    def get_service(self, name):
        return self._svc.get(name)


def _inbound(text, *, user, raw=None, surface="email", chat="c1", thread=None):
    src = SessionSource(surface_id=surface, chat_id=chat, chat_type="dm", thread_id=thread)
    ident = Identity(user_id=user, source=src, raw_user_id=raw if raw is not None else user)
    return InboundMessage(text=text, identity=ident)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("POLYROB_REQUIRE_PAIRING", "POLYROB_LOCAL", "POLYROB_OWNER_USER_ID",
              "SURFACE_SUPER_ADMIN_USER_IDS", "CORRESPONDENT_ACCESS_ENABLED",
              "SESSION_RESET_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SESSION_RESET_MODE", "none")  # isolate from idle-boundary policy


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical(tmp_path, monkeypatch):
    """Default OFF: an unknown non-owner on a NON-forgeable surface still routes
    TASK_AGENT (no tier denial). Email is excluded — see the P1-6 test below."""
    corr = CorrespondentRegistry(str(tmp_path / "corr.db"))
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")), corr_reg=corr)
    d = await route_inbound(c, _inbound("hello", user="u_stranger", surface="telegram"))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_p1_6_email_denied_when_flag_off(tmp_path, monkeypatch):
    """P1-6: with the correspondent model OFF, a forgeable-sender surface (email) must
    NOT fall through to the obey-path — it is correspondent-or-denied by construction
    (owner-by-email is off in v1). This holds regardless of any CLI env setdefault."""
    monkeypatch.delenv("CORRESPONDENT_ACCESS_ENABLED", raising=False)  # model OFF
    corr = CorrespondentRegistry(str(tmp_path / "corr.db"))
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")), corr_reg=corr)
    d = await route_inbound(c, _inbound("let me in", user="u_stranger", surface="email"))
    assert d.kind == RouteKind.DENIED


@pytest.mark.asyncio
async def test_owner_flows_normally_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    corr = CorrespondentRegistry(str(tmp_path / "corr.db"))
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")), corr_reg=corr)
    d = await route_inbound(c, _inbound("do the thing", user="u_owner"))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_correspondent_routes_to_originating_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    corr = CorrespondentRegistry(str(tmp_path / "corr.db"))
    corr.seed(surface="email", address="john@acme.com", session_id="orig_sess",
              user_id="u_owner", thread_id="t1", provenance="owner", require_approval=False)
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")), corr_reg=corr)
    msg = _inbound("the invoice is paid", user="u_john", raw="john@acme.com", thread="t1")
    d = await route_inbound(c, msg)
    assert d.kind == RouteKind.CORRESPONDENT_DATA
    assert d.session_id == "orig_sess"  # delivered to the session that contacted them


@pytest.mark.asyncio
async def test_correspondent_cannot_issue_command(tmp_path, monkeypatch):
    """A correspondent's '/cancel' is DATA, never a COMMAND (closed tier table)."""
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    corr = CorrespondentRegistry(str(tmp_path / "corr.db"))
    corr.seed(surface="email", address="john@acme.com", session_id="orig_sess",
              user_id="u_owner", thread_id="t1", provenance="owner", require_approval=False)
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")), corr_reg=corr)
    msg = _inbound("/cancel", user="u_john", raw="john@acme.com", thread="t1")
    d = await route_inbound(c, msg)
    assert d.kind == RouteKind.CORRESPONDENT_DATA
    assert d.command is None


@pytest.mark.asyncio
async def test_tier_fault_fails_closed_to_denied_not_steer(tmp_path, monkeypatch):
    """Fusion CRITICAL: a fault in the tier block must DENY, never fall through to the
    legacy obey-path (STEER/TASK_AGENT) for a sender the access model was gating."""
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")

    def _boom(*a, **k):
        raise RuntimeError("tier resolver crashed")

    # Force a fault in the dispatcher's tier block (not the registry, which access.py
    # already swallows fail-closed) — the dispatcher must DENY, not fall through.
    monkeypatch.setattr("core.surfaces.access.resolve_access_tier", _boom)
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")),
                   corr_reg=CorrespondentRegistry(str(tmp_path / "corr.db")))
    d = await route_inbound(c, _inbound("let me in", user="u_stranger", raw="x@evil.com"))
    assert d.kind == RouteKind.DENIED


@pytest.mark.asyncio
async def test_unknown_non_owner_denied_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    corr = CorrespondentRegistry(str(tmp_path / "corr.db"))
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")), corr_reg=corr)
    d = await route_inbound(c, _inbound("let me in", user="u_stranger", raw="x@evil.com"))
    assert d.kind == RouteKind.DENIED


# ---------------------------------------------------------------------------
# T1.5: dead-target revive on inbound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbound_revives_both_identity_tuples(tmp_path):
    """An inbound message clears BOTH the chat-id and sender-id dead-target rows
    (outbound dest is the chat; inbound sender is the user — they differ in groups),
    and routing proceeds exactly as it would without the registry."""
    from core.surfaces.dead_targets import DeadTargetStore

    dt = DeadTargetStore(str(tmp_path / "dead.db"))
    dt.mark("telegram", "c1", "blocked")       # chat_id tuple
    dt.mark("telegram", "u_john", "blocked")   # sender tuple
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")),
                   dead_targets=dt)
    msg = _inbound("hello again", user="u_john", surface="telegram", chat="c1")
    d = await route_inbound(c, msg)
    assert dt.is_dead("telegram", "c1") is False
    assert dt.is_dead("telegram", "u_john") is False
    assert d.kind == RouteKind.TASK_AGENT  # routing outcome unchanged by the revive


@pytest.mark.asyncio
async def test_inbound_revive_noop_when_registry_absent(tmp_path):
    """No dead_targets service registered -> revive is a silent no-op; routing is
    byte-identical."""
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")))
    msg = _inbound("hi", user="u_x", surface="telegram", chat="c1")
    d = await route_inbound(c, msg)
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_inbound_revive_fail_open_when_store_raises(tmp_path):
    """A raising dead_targets service must never block routing (fail-open)."""
    class _BoomStore:
        def clear(self, surface, address):
            raise RuntimeError("db locked")

    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "chat.db")),
                   dead_targets=_BoomStore())
    msg = _inbound("hello", user="u_stranger", surface="telegram", chat="c1")
    d = await route_inbound(c, msg)
    assert d.kind == RouteKind.TASK_AGENT  # routing proceeds despite the store fault
