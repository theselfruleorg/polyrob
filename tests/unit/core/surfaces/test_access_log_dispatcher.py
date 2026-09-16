"""045 lane 1: route_inbound records EVERY decision, and a raising recorder
leaves the decision byte-identical."""
import json

import pytest

from core.surfaces.dispatcher import RouteKind, route_inbound
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource


def _inbound(text="hello", user="u_x", surface="telegram", chat="c1"):
    src = SessionSource(surface_id=surface, chat_id=chat, chat_type="dm")
    return InboundMessage(text=text,
                          identity=Identity(user_id=user, source=src, raw_user_id="42"))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("POLYROB_REQUIRE_PAIRING", "POLYROB_LOCAL", "POLYROB_OWNER_USER_ID",
              "SURFACE_SUPER_ADMIN_USER_IDS", "CORRESPONDENT_ACCESS_ENABLED",
              "SESSION_RESET_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SESSION_RESET_MODE", "none")  # isolate from idle-boundary policy


@pytest.fixture
def tele_db(tmp_path, monkeypatch):
    p = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(p))
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    el._INSTANCES.clear()
    yield str(p)
    el._INSTANCES.clear()


def _rows(db_path):
    from core.sqlite_util import execute_retry
    return [dict(r) for r in (execute_retry(
        db_path, "SELECT kind, attrs FROM telemetry_events", (), fetch="all") or [])]


@pytest.mark.asyncio
async def test_every_route_is_recorded(tele_db):
    d = await route_inbound(None, _inbound("hello there"))
    assert d.kind in set(RouteKind)
    rows = _rows(tele_db)
    assert len(rows) == 1
    assert rows[0]["kind"] in ("inbound_routed", "access_denied")
    assert json.loads(rows[0]["attrs"])["surface"] == "telegram"


@pytest.mark.asyncio
async def test_command_route_is_recorded(tele_db):
    d = await route_inbound(None, _inbound("/help"))
    assert d.kind == RouteKind.COMMAND
    assert json.loads(_rows(tele_db)[0]["attrs"])["decision"] == "command"


@pytest.mark.asyncio
async def test_a_raising_recorder_does_not_change_the_decision(tele_db, monkeypatch):
    import core.surfaces.dispatcher as disp

    baseline = await route_inbound(None, _inbound("hello there"))

    def _boom(*a, **k):
        raise RuntimeError("recorder down")

    monkeypatch.setattr(disp, "record_route", _boom)
    after = await route_inbound(None, _inbound("hello there"))

    assert after.kind == baseline.kind
    assert after.session_key == baseline.session_key
    assert after.silent == baseline.silent
    assert after.session_id == baseline.session_id


@pytest.fixture(autouse=True)
def _reset_owner_tenant_cache():
    """The perimeter tenant is resolved once and cached for the process — a
    test that changes the owner env must not read the previous test's answer."""
    from core.surfaces import access_log
    access_log._OWNER_TENANT = None
    yield
    access_log._OWNER_TENANT = None


@pytest.mark.asyncio
async def test_a_strangers_traffic_is_visible_to_the_owners_rollup(
        tele_db, tmp_path, monkeypatch):
    """C1 — producer and reader must agree on what `user_id` means.

    The perimeter used to stamp the SENDER's surface-hashed id as the row's
    tenant, while every owner seat reads the rollup with the OWNER's id. A
    store holding a stranger's inbound, a route denial and an allowlist drop
    therefore rolled up as `inbound=0 denied=0` on the only seat that matters.

    This is the test no per-task review could have caught: the producer and the
    reader are exercised in ONE test, because each passes green on its own
    against a different model of the same row.
    """
    import time

    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner_abc")
    monkeypatch.delenv("GROUP_CHAT_ENABLED", raising=False)

    # 1) a stranger's DM — routed
    src = SessionSource(surface_id="telegram", chat_id="c7", chat_type="dm")
    await route_inbound(None, InboundMessage(
        text="hello there",
        identity=Identity(user_id="u_deadbeef", source=src, raw_user_id="5551")))

    # 2) a stranger in a group while GROUP_CHAT_ENABLED is off — denied
    gsrc = SessionSource(surface_id="telegram", chat_id="g9", chat_type="group")
    d = await route_inbound(None, InboundMessage(
        text="hello room",
        identity=Identity(user_id="u_deadbeef", source=gsrc, raw_user_id="5551")))
    assert d.kind == RouteKind.DENIED

    # 3) the raw allowlist drop that never reaches route_inbound at all
    from core.surfaces.access_log import record_pre_route_drop
    record_pre_route_drop(surface="telegram", chat_id="c7", chat_type="dm",
                          sender="5551", reason="raw_allowlist", body_len=9)

    from core.security_digest import build_security_rollup
    r = build_security_rollup("owner_abc", data_dir=str(tmp_path),
                              since_ts=time.time() - 3600, window_sec=3600)
    assert r.inbound == 1, "the stranger's routed message is invisible to the owner"
    assert r.denied == 2, "the denials are invisible to the owner"
    assert ("5551", 3) in r.top_senders
    assert ("raw_allowlist", 1) in r.top_denial_reasons


@pytest.mark.asyncio
async def test_the_row_tenant_is_the_deployment_not_the_sender(tele_db, monkeypatch):
    """The counterparty keeps its own home (`attrs.sender`); the tenant column
    is the deployment, so every owner seat can read the row back."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner_abc")
    await route_inbound(None, _inbound("hello there", user="u_deadbeef"))
    from core.sqlite_util import execute_retry
    rows = [dict(r) for r in (execute_retry(
        tele_db, "SELECT user_id, attrs FROM telemetry_events", (),
        fetch="all") or [])]
    assert rows[0]["user_id"] == "owner_abc"
    assert json.loads(rows[0]["attrs"])["sender"] == "42"
