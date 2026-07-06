"""P3: route_inbound — the inbound decision table (pure, transport-free).

Precedence (Fusion correction): COMMAND > STEER > TASK_AGENT/CHAT_FASTPATH.
COMMAND must win over an active session, else /cancel mid-session would be fed to
the agent as a steering message instead of cancelling it.

cold/warm = SessionChatRegistry row existence (single SSOT, durable). STEER whenever
a row resolves (sticky: a warm chat NEVER diverts to the ChatAgent fast-path, even a
warm-but-dead one -> the caller rehydrates the same session_key). The ChatAgent
fast-path is a default-OFF cost optimization, reachable only when CHAT_INTENT_CLASSIFIER
is ON, the session is cold, and an injected is_chitchat predicate says so.
"""
import pytest

from core.surfaces.dispatcher import route_inbound, RouteKind
from core.surfaces.envelopes import InboundMessage, Identity, SessionSource
from core.surfaces.session_chat_registry import SessionChatRegistry, build_session_key


class _FakeContainer:
    def __init__(self, registry=None):
        self._svc = {"session_chat_registry": registry} if registry else {}

    def get_service(self, name):
        return self._svc.get(name)


def _inbound(text, surface="telegram", chat="555", user="u_abc", chat_type="dm"):
    src = SessionSource(surface_id=surface, chat_id=chat, chat_type=chat_type)
    return InboundMessage(text=text, identity=Identity(user_id=user, source=src))


def _registry_with(tmp_path, *, warm_key=None):
    reg = SessionChatRegistry(str(tmp_path / "chat.db"))
    if warm_key:
        reg.bind(warm_key, "sess_1", "u_abc", "telegram", "555")
    return reg


@pytest.mark.asyncio
async def test_command_wins_over_active_session(tmp_path):
    """/cancel during a WARM session => COMMAND, not STEER (the precedence fix)."""
    msg = _inbound("/cancel")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    c = _FakeContainer(_registry_with(tmp_path, warm_key=key))
    d = await route_inbound(c, msg)
    assert d.kind == RouteKind.COMMAND
    assert d.command == "/cancel"


@pytest.mark.asyncio
async def test_command_carries_session_id_from_binding(tmp_path):
    """#1: a COMMAND on a warm chat must carry the bound session_id, else /cancel and
    /new are no-ops (they read decision.session_id)."""
    msg = _inbound("/cancel")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    c = _FakeContainer(_registry_with(tmp_path, warm_key=key))
    d = await route_inbound(c, msg)
    assert d.kind == RouteKind.COMMAND and d.command == "/cancel"
    assert d.session_id == "sess_1"  # resolved from the binding


@pytest.mark.asyncio
async def test_known_command_on_cold(tmp_path):
    c = _FakeContainer(_registry_with(tmp_path))
    d = await route_inbound(c, _inbound("/task do the thing"))
    assert d.kind == RouteKind.COMMAND and d.command == "/task"


@pytest.mark.asyncio
async def test_unknown_slash_is_not_a_command(tmp_path):
    c = _FakeContainer(_registry_with(tmp_path))
    d = await route_inbound(c, _inbound("/wat"))
    assert d.kind == RouteKind.TASK_AGENT  # falls through (cold, classifier off)


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["/allow", "/deny", "/allowlist"])
async def test_outbound_allowlist_verbs_classify_as_command(tmp_path, verb):
    """The outbound-allowlist owner-admin verbs must win over an active session,
    like the other owner-admin verbs — else the surface handler never sees them."""
    msg = _inbound(f"{verb} telegram 555")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    c = _FakeContainer(_registry_with(tmp_path, warm_key=key))
    d = await route_inbound(c, msg)
    assert d.kind == RouteKind.COMMAND and d.command == verb


@pytest.mark.asyncio
async def test_warm_session_steers(tmp_path):
    msg = _inbound("keep going please")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    c = _FakeContainer(_registry_with(tmp_path, warm_key=key))
    d = await route_inbound(c, msg)
    assert d.kind == RouteKind.STEER
    assert d.session_id == "sess_1"
    assert d.session_key == key


@pytest.mark.asyncio
async def test_cold_classifier_off_goes_to_task_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAT_INTENT_CLASSIFIER", raising=False)
    c = _FakeContainer(_registry_with(tmp_path))
    d = await route_inbound(c, _inbound("hi there"), is_chitchat=lambda m: True)
    assert d.kind == RouteKind.TASK_AGENT  # classifier OFF => never fast-path


@pytest.mark.asyncio
async def test_cold_classifier_on_chitchat_fastpath(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_INTENT_CLASSIFIER", "true")
    c = _FakeContainer(_registry_with(tmp_path))
    d = await route_inbound(c, _inbound("hello!"), is_chitchat=lambda m: True)
    assert d.kind == RouteKind.CHAT_FASTPATH


@pytest.mark.asyncio
async def test_cold_classifier_on_nonchitchat_task_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_INTENT_CLASSIFIER", "true")
    c = _FakeContainer(_registry_with(tmp_path))
    d = await route_inbound(c, _inbound("scrape this site"), is_chitchat=lambda m: False)
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_warm_never_diverts_even_with_chitchat_classifier(tmp_path, monkeypatch):
    """Sticky invariant: a warm chat steers, never fast-paths (no two-history fork)."""
    monkeypatch.setenv("CHAT_INTENT_CLASSIFIER", "true")
    msg = _inbound("hello!")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    c = _FakeContainer(_registry_with(tmp_path, warm_key=key))
    d = await route_inbound(c, msg, is_chitchat=lambda m: True)
    assert d.kind == RouteKind.STEER  # warm wins over fast-path


@pytest.mark.asyncio
async def test_no_registry_is_cold_failopen(tmp_path):
    c = _FakeContainer(registry=None)
    d = await route_inbound(c, _inbound("anything"))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_async_chitchat_predicate_awaited(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_INTENT_CLASSIFIER", "true")
    c = _FakeContainer(_registry_with(tmp_path))

    async def _async_chitchat(m):
        return True

    d = await route_inbound(c, _inbound("yo"), is_chitchat=_async_chitchat)
    assert d.kind == RouteKind.CHAT_FASTPATH


@pytest.mark.asyncio
async def test_dm_key_is_user_isolated(tmp_path):
    """The dispatcher resolves against the chat-scoped DM key (user-isolated)."""
    msg = _inbound("hi", chat_type="dm", user="u_xyz")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    assert key.endswith(":u_xyz")
    c = _FakeContainer(_registry_with(tmp_path, warm_key=key))
    d = await route_inbound(c, msg)
    assert d.kind == RouteKind.STEER  # resolves the user-isolated key


# --- polyrob D3: ingress pairing gate ---------------------------------------
import types as _types


class _ContainerWithConfig:
    def __init__(self, tmp_path, registry=None):
        self._svc = {"session_chat_registry": registry} if registry else {}
        self.config = _types.SimpleNamespace(data_dir=str(tmp_path))

    def get_service(self, name):
        return self._svc.get(name)


@pytest.mark.asyncio
async def test_pairing_off_is_byte_identical(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYROB_REQUIRE_PAIRING", raising=False)
    c = _ContainerWithConfig(tmp_path, _registry_with(tmp_path))
    d = await route_inbound(c, _inbound("hello there"))
    assert d.kind == RouteKind.TASK_AGENT  # not DENIED


@pytest.mark.asyncio
async def test_unpaired_denied_when_pairing_on(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("SURFACE_SUPER_ADMIN_USER_IDS", raising=False)
    c = _ContainerWithConfig(tmp_path, _registry_with(tmp_path))
    d = await route_inbound(c, _inbound("hello", user="stranger"))
    assert d.kind == RouteKind.DENIED
    assert d.pairing_code  # issued so the operator can approve


@pytest.mark.asyncio
async def test_owner_allowed_when_pairing_on(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_REQUIRE_PAIRING", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "the-owner")
    c = _ContainerWithConfig(tmp_path, _registry_with(tmp_path))
    d = await route_inbound(c, _inbound("hello", user="the-owner"))
    assert d.kind == RouteKind.TASK_AGENT  # owner bypasses pairing


# --- P0.1 session-boundary policy -------------------------------------------

@pytest.mark.asyncio
async def test_idle_boundary_starts_fresh(tmp_path, monkeypatch):
    """An idle warm session (mode=idle) -> the next message starts fresh (TASK_AGENT)."""
    monkeypatch.setenv("SESSION_RESET_MODE", "idle")
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "1")
    msg = _inbound("hi there")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    reg = _registry_with(tmp_path, warm_key=key)
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "chat.db"))
    conn.execute("UPDATE session_chat_map SET updated_at=? WHERE session_key=?", (1000.0, key))
    conn.commit(); conn.close()
    d = await route_inbound(_FakeContainer(reg), msg)
    assert d.kind == RouteKind.TASK_AGENT  # idle -> fresh


@pytest.mark.asyncio
async def test_default_mode_idle_rolls_stale_warm_session(tmp_path, monkeypatch):
    """#7: SESSION_RESET_MODE now defaults to `idle` (server flip) — an ancient warm
    row starts fresh (TASK_AGENT) instead of STEERing into a long-stale thread."""
    monkeypatch.delenv("SESSION_RESET_MODE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    msg = _inbound("continue")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    reg = _registry_with(tmp_path, warm_key=key)
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "chat.db"))
    conn.execute("UPDATE session_chat_map SET updated_at=? WHERE session_key=?", (1000.0, key))
    conn.commit(); conn.close()
    d = await route_inbound(_FakeContainer(reg), msg)
    assert d.kind == RouteKind.TASK_AGENT  # idle default -> stale row rolls fresh


@pytest.mark.asyncio
async def test_explicit_none_keeps_steer_for_stale_row(tmp_path, monkeypatch):
    """Operators can still pin the legacy inert behavior: none -> a warm row always
    STEERs, however stale."""
    monkeypatch.setenv("SESSION_RESET_MODE", "none")
    msg = _inbound("continue")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    reg = _registry_with(tmp_path, warm_key=key)
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "chat.db"))
    conn.execute("UPDATE session_chat_map SET updated_at=? WHERE session_key=?", (1000.0, key))
    conn.commit(); conn.close()
    d = await route_inbound(_FakeContainer(reg), msg)
    assert d.kind == RouteKind.STEER  # explicit inert


@pytest.mark.asyncio
async def test_route_steer_does_not_touch_registry(tmp_path, monkeypatch):
    """a1: route_inbound stays a pure decision table — it must NOT bump updated_at.
    Last-activity is bumped on the delivery-success path (TaskAgent.touch_chat_binding)."""
    monkeypatch.setenv("SESSION_RESET_MODE", "idle")
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "1440")  # 24h, won't trip
    msg = _inbound("ping")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    reg = _registry_with(tmp_path, warm_key=key)
    import sqlite3, time
    old = time.time() - 1000
    conn = sqlite3.connect(str(tmp_path / "chat.db"))
    conn.execute("UPDATE session_chat_map SET updated_at=? WHERE session_key=?", (old, key))
    conn.commit(); conn.close()
    d = await route_inbound(_FakeContainer(reg), msg)
    assert d.kind == RouteKind.STEER
    after = float(reg.resolve(key)["updated_at"])
    assert abs(after - old) < 1  # unchanged: the router did NOT touch
