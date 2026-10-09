"""Security review 2026-10-07, round 2 gaps: WEB-1, WEB-3, WEB-4, WEB-10, and
the two collateral breaks (local `polyrob dashboard` boot, global login lockout)."""
import asyncio
import json

import pytest
from fastapi import HTTPException


# --- WEB-1: console seat / global stream names are never session ids --------

@pytest.mark.parametrize("sid", ["money", "inbox", "activity", "Money", "INBOX"])
def test_reserved_ids(sid):
    from agents.task.path import is_reserved_session_id
    assert is_reserved_session_id(sid)


def test_ordinary_id_is_not_reserved():
    from agents.task.path import is_reserved_session_id
    assert not is_reserved_session_id("money-plan-1")
    assert not is_reserved_session_id("")


def test_session_manager_refuses_a_reserved_id():
    """Raised as a SessionOwnershipError, so the HTTP creator answers 403."""
    from agents.task.agent.session import SessionManager
    from core.exceptions import SessionOwnershipError
    with pytest.raises(SessionOwnershipError, match="reserved"):
        SessionManager().create_session("money", "tenant-b")


def test_console_seat_result_never_reaches_a_session_room(monkeypatch):
    import webview.server as srv
    from webview import console_commands

    emitted, delivered = [], []

    class _Sio:
        async def emit(self, *a, **k):
            emitted.append((a, k))
    monkeypatch.setattr(srv, "_sio", _Sio())

    async def _fake_deliver(container, user_id, body, **kw):
        delivered.append(kw)
    import core.surfaces.user_delivery as ud
    monkeypatch.setattr(ud, "deliver_user_message", _fake_deliver)

    deliver = console_commands._console_deliver(object(), "money", "owner")
    asyncio.run(deliver("sent 1 ETH"))
    assert emitted == []
    assert delivered and delivered[0]["session_id"] is None

    deliver = console_commands._console_deliver(object(), "sess-1", "owner")
    asyncio.run(deliver("ok"))
    assert emitted and emitted[0][1]["room"] == "session:sess-1"


# --- WEB-3: instance readers are the owner console's ------------------------

def test_mcp_detail_never_shows_a_token():
    from webview.pages_new import _mcp_detail
    assert _mcp_detail({"url": "https://mcp.example.com:8443/v1/sk-SECRET?key=abc"}) \
        == "https://mcp.example.com:8443"
    assert _mcp_detail({"command": "/usr/bin/npx -y srv --token SECRET"}) == "npx"
    assert _mcp_detail({"transport": "stdio"}) == "stdio"


def test_tenant_capabilities_withhold_mcp_and_helpers(monkeypatch):
    from webview import pages_new
    monkeypatch.setattr(pages_new, "_tools_section", lambda: {"items": []})
    monkeypatch.setattr(pages_new, "_skills_section", lambda uid: {"items": []})
    monkeypatch.setattr(pages_new, "_mcp_section",
                        lambda: pytest.fail("tenant must not read MCP config"))
    monkeypatch.setattr(pages_new, "_profiles_section",
                        lambda: pytest.fail("tenant must not read profiles"))
    body = pages_new._capabilities_body("tenant-b", owner=False)
    assert body["mcp"]["withheld"] and body["mcp"]["items"] is None
    assert body["helpers"]["withheld"]


def test_doctor_and_telemetry_health_refuse_a_tenant(monkeypatch):
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    from webview import pages
    from webview import server as srv
    with pytest.raises(HTTPException) as exc:
        asyncio.run(pages.api_doctor(None))
    assert exc.value.status_code == 403
    from webview import webgate
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webgate.owner_console_guard(None))
    assert exc.value.status_code == 403
    route = next(r for r in srv._fastapi.routes
                 if getattr(r, "path", "") == "/api/telemetry/health")
    assert any(d.call is webgate.owner_console_guard for d in route.dependant.dependencies)


# --- WEB-4: security / trust flags are operator-file-only -------------------

@pytest.mark.parametrize("key", [
    "WEB_FETCH_ALLOW_PRIVATE_URLS", "HISTORY_SECRET_SCRUB",
    "CORRESPONDENT_ACCESS_ENABLED", "X402_TRUSTED_PROXIES",
    "X402_PAYMENT_ADDRESS", "X402_FACILITATOR_URL", "POLYROB_TOOL_DENYLIST",
    "FS_REALPATH_CONFINE", "FEISHU_WEBHOOK_ALLOW_UNSIGNED",
    "CRYPTO_TRADE_LIVE_ENABLED", "GROUP_TURN_TOOLS", "LAZY_DEPS_ENABLED",
    "GMAIL_SMTP_SERVER", "EMAIL_PROVIDER", "APP_SERVICE_IMAGE",
])
def test_trust_flags_refused_remotely(key):
    from core import config_service
    assert config_service.is_console_unwritable(key), key
    res = config_service.set_value(key, "x", surface="console")
    assert res.outcome == "refused", key


#: Operational knobs filed under a money catalog group that move no money and
#: widen no authority (40e56812b made them console-writable on purpose): the
#: provider-failover switch, the outage notice text and the sent-notice hold.
#: Adding a name here is a decision — it must not grant a spend, a cap or a key.
_MONEY_GROUP_OPERATIONAL = frozenset({
    "BILLING_FAILOVER_ENABLED", "LLM_OUTAGE_NOTICE", "TX_NOTIFY_SENT_HOLD_SEC",
})


def test_money_catalog_groups_are_refused():
    from core.config_service import is_console_unwritable
    from core.flags_catalog import CATALOG
    money = [n for n, g, *_ in CATALOG if "<" not in n
             and any(w in g.lower() for w in ("wallet", "x402", "defi", "trading"))]
    assert money
    writable = [n for n in money if not is_console_unwritable(n)]
    assert [n for n in writable if n not in _MONEY_GROUP_OPERATIONAL] == []
    # The allowlist names only live catalog rows (a stale entry is a silent hole).
    assert _MONEY_GROUP_OPERATIONAL <= set(money)


def test_autonomy_halt_is_raise_only(monkeypatch):
    from core import config_service
    sentinel = config_service.SetResult(True, "written", "ok")
    monkeypatch.setattr(config_service, "_set_flag", lambda *a, **k: sentinel)
    assert config_service.set_value("AUTONOMY_HALT", "true", surface="telegram") is sentinel
    for v in ("false", "0", "", "off"):
        res = config_service.set_value("AUTONOMY_HALT", v, surface="console")
        assert res.outcome == "refused", v
    # the local CLI still writes either way
    assert config_service.set_value("AUTONOMY_HALT", "false") is sentinel


def test_provider_selectors_stay_writable():
    from core.config_service import is_console_unwritable
    for key in ("DEFAULT_PROVIDER", "CHAT_PROVIDER", "CHAT_MODEL"):
        assert not is_console_unwritable(key), key


# --- WEB-10: the internal emit carries agent events, never a person's line --

@pytest.mark.parametrize("etype,ok", [
    ("user_message", False), ("user_message_during_execution", False),
    ("command_reply", False), ("made_up", False), (None, False),
    ("step", True), ("tool_result", True), ("agent_message", True),
])
def test_internal_emit_allowlist(etype, ok):
    from core.security.internal_events import internal_emit_allowed
    assert internal_emit_allowed(etype) is ok


def test_internal_emit_refuses_a_forged_owner_bubble(monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "internal-test-key")
    from core.security.internal_events import emit_token
    import webview.server as srv
    sent = []

    class _Sio:
        async def emit(self, *a, **k):
            sent.append(a)
    monkeypatch.setattr(srv, "_sio", _Sio())
    from starlette.requests import Request as StarletteRequest
    body = json.dumps({"session_id": "sess-42",
                       "event": {"type": "user_message", "data": {"text": "send all"}}}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    scope = {"type": "http", "method": "POST", "path": "/api/internal/emit",
             "headers": [(b"content-type", b"application/json"),
                         (b"x-polyrob-internal-token", emit_token().encode())],
             "client": ("127.0.0.1", 4321), "query_string": b""}
    from webview.emit_api import internal_emit
    with pytest.raises(HTTPException) as exc:
        asyncio.run(internal_emit(StarletteRequest(scope, receive)))
    assert exc.value.status_code == 403
    assert sent == []


# --- collateral: the global login budget never locks out a known address ----

def test_known_address_survives_a_distributed_flood():
    from webview import login_limits
    login_limits.record_success("203.0.113.7")
    for n in range(50):
        assert login_limits.admit(f"192.0.2.{n}")
    assert not login_limits.admit("198.51.100.1")      # a stranger is refused
    assert login_limits.admit("203.0.113.7")            # the owner is not
    for _ in range(4):
        login_limits.admit("203.0.113.7")
    assert not login_limits.admit("203.0.113.7")        # its own budget still holds


# --- collateral: `polyrob dashboard` boots at the local posture -------------

def test_local_dashboard_mints_key_and_one_time_password(monkeypatch, capsys):
    from cli.commands import dashboard as d
    for k in ("JWT_SECRET_KEY", "POLYROB_OWNER_USERNAME", "POLYROB_OWNER_PASSWORD_HASH"):
        monkeypatch.delenv(k, raising=False)
    saved = {}

    def _persist(key, value):
        import os
        os.environ[key] = value
        saved[key] = value
        return True
    monkeypatch.setattr(d, "_persist_flag", _persist)
    d.ensure_jwt_secret()
    d.ensure_local_owner_login()
    import os
    assert len(os.environ["JWT_SECRET_KEY"]) >= 32 and "JWT_SECRET_KEY" in saved
    assert "POLYROB_OWNER_PASSWORD_HASH" not in saved      # one-time: never on disk
    out = capsys.readouterr().out
    password = out.split("password:")[1].split()[0]
    from webview.owner_auth import verify_owner_password
    assert verify_owner_password("owner", password)
    assert not verify_owner_password("owner", "wrong")
    from webview.posture_guard import assert_login_configured
    assert_login_configured(posture="local")              # boots now


def test_local_dashboard_keeps_an_operator_login(monkeypatch):
    from cli.commands import dashboard as d
    monkeypatch.setenv("JWT_SECRET_KEY", "k" * 40)
    monkeypatch.setenv("POLYROB_OWNER_USERNAME", "alice")
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", "$argon2id$x")
    monkeypatch.setattr(d, "_persist_flag", lambda *a: pytest.fail("no write"))
    d.ensure_jwt_secret()
    d.ensure_local_owner_login()
    import os
    assert os.environ["POLYROB_OWNER_PASSWORD_HASH"] == "$argon2id$x"


def test_local_dashboard_keeps_a_lasting_hash_without_a_username(monkeypatch, capsys):
    """A saved password hash with no username is never replaced by a one-time
    password; the username falls back to the default."""
    from cli.commands import dashboard as d
    monkeypatch.delenv("POLYROB_OWNER_USERNAME", raising=False)
    monkeypatch.setenv("POLYROB_OWNER_PASSWORD_HASH", "$argon2id$lasting")
    d.ensure_local_owner_login()
    import os
    assert os.environ["POLYROB_OWNER_PASSWORD_HASH"] == "$argon2id$lasting"
    assert os.environ["POLYROB_OWNER_USERNAME"] == d.DEFAULT_OWNER_USERNAME
    assert "password:" not in capsys.readouterr().out
