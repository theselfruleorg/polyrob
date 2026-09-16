"""043 WS-AB1 — the Agent destination's read seams (capabilities/memory/flags).

Three JSON readers on ``api_router`` (mounted in BOTH UIs, not the page switch),
each tenant-scoped and read-only. What is tested here is what only these seams
can get wrong: a tool's honest on/off + source, a memory search that stays inside
one tenant, and a flag catalog that stays dark until you ask and MARKS the flags
the console may never write.
"""
import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import webview.pages_new as mod


# --- capabilities ----------------------------------------------------------- #

def test_capabilities_lists_a_tool_with_on_off_and_source():
    body = mod._capabilities_body("u1")
    assert body["tools"]["error"] is None
    by_id = {r["id"]: r for r in body["tools"]["items"]}

    # A default, always-on tool: on with source "default".
    fs = by_id["filesystem"]
    assert fs["on"] is True
    assert fs["default"] is True
    assert fs["source"] == "default"
    assert fs["risk"] == "low"

    # A gated money tool that is OFF on this deploy: on=False, and its money
    # capability + high risk are named (so the console can say WHY it is off).
    dt = by_id["defi_trade"]
    assert dt["on"] is False
    assert "money" in dt["capabilities"]
    assert dt["risk"] == "high"


def test_capabilities_never_faked_last_used_for_a_tool():
    """Per-tool usage is not tracked anywhere — a confident timestamp would be
    invented, so it stays None."""
    body = mod._capabilities_body("u1")
    assert all(r["last_used"] is None for r in body["tools"]["items"])


def test_capabilities_sections_are_independently_guarded():
    body = mod._capabilities_body("u1")
    assert set(body) == {"tools", "skills", "mcp", "helpers"}
    for name in ("tools", "skills", "mcp", "helpers"):
        assert "items" in body[name]


def test_the_capabilities_endpoint_scopes_to_the_tenant(monkeypatch):
    monkeypatch.setattr("webview.pages._effective_user_id", lambda request: "u1")
    seen = {}
    monkeypatch.setattr(mod, "_capabilities_body",
                        lambda uid: seen.update(uid=uid) or {"tools": {"items": []}})
    app = FastAPI()
    app.include_router(mod.api_router)
    resp = TestClient(app).get("/api/webgate/capabilities")
    assert resp.status_code == 200
    assert seen == {"uid": "u1"}


# --- memory search (recall + curated notes, tenant-scoped) ------------------ #

@pytest.fixture()
def memory_client(monkeypatch):
    """A real SqliteMemoryProvider over a temp db, wired behind the reader."""
    from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
    d = tempfile.mkdtemp()
    provider = SqliteMemoryProvider(db_path=os.path.join(d, "memory.db"))
    monkeypatch.setattr("webview.pages._memory_provider", lambda: provider)
    return provider


def test_memory_search_returns_tenant_scoped_rows(memory_client, monkeypatch):
    import asyncio

    asyncio.run(
        memory_client.note_create("u1", "remember the milk", title="groceries"))
    asyncio.run(
        memory_client.note_create("u2", "another tenant's secret note"))

    monkeypatch.setattr("webview.pages._effective_user_id", lambda request: "u1")
    app = FastAPI()
    app.include_router(mod.api_router)
    resp = TestClient(app).get("/api/webgate/memory/search")
    assert resp.status_code == 200
    data = resp.json()
    contents = [n["content"] for n in data["notes"]]
    assert "remember the milk" in contents
    assert "another tenant's secret note" not in contents  # never crosses tenants


def test_memory_search_names_a_missing_provider_never_a_confident_empty(monkeypatch):
    monkeypatch.setattr("webview.pages._memory_provider", lambda: None)
    monkeypatch.setattr("webview.pages._effective_user_id", lambda request: "u1")
    app = FastAPI()
    app.include_router(mod.api_router)
    data = TestClient(app).get("/api/webgate/memory/search").json()
    assert data["unavailable"] is True
    assert data["provider"] is None


def test_memory_search_uses_a_distinct_path_from_the_legacy_reader():
    """The legacy ``GET /api/webgate/memory`` (recall-only) is kept for the legacy
    console; a second GET there would be a silently-dead route. So this reader is
    at ``/api/webgate/memory/search``."""
    paths = {r.path for r in mod.api_router.routes}
    assert "/api/webgate/memory/search" in paths
    assert "/api/webgate/memory" not in {
        r.path for r in mod.api_router.routes
        if "GET" in (getattr(r, "methods", None) or set())}


# --- flags (search-first, guarded marked) ----------------------------------- #

def test_flags_return_nothing_until_a_query_or_group():
    body = mod._flags_body("", "")
    assert body["queried"] is False
    assert body["flags"] == []
    # …but the groups are offered so the UI can present a picker.
    assert isinstance(body["groups"], list) and body["groups"]


def test_flags_search_marks_guarded_and_secret_keys():
    body = mod._flags_body("WEBVIEW_READ_ONLY", "")
    assert body["queried"] is True
    row = next(f for f in body["flags"] if f["name"] == "WEBVIEW_READ_ONLY")
    # Trust-posture / console-gating flags are never console-writable.
    assert row["guarded"] is True
    assert row["secret"] is False

    secret = mod._flags_body("OPENAI_API_KEY", "")
    key = next(f for f in secret["flags"] if f["name"] == "OPENAI_API_KEY")
    assert key["guarded"] is True and key["secret"] is True


def test_flags_ordinary_knob_is_not_guarded():
    body = mod._flags_body("GOALS_ENABLED", "")
    row = next(f for f in body["flags"] if f["name"] == "GOALS_ENABLED")
    assert row["guarded"] is False


def test_flags_group_filter_alone_returns_rows():
    """A group with no query still returns that group's flags (search-first is
    satisfied by EITHER a query OR a group)."""
    groups = mod._flag_groups()
    body = mod._flags_body("", groups[0])
    assert body["queried"] is True
    assert all(f["group"] == groups[0] for f in body["flags"])


# --- both UIs, no write guard on a read ------------------------------------- #

def test_the_agent_readers_ride_api_router_in_both_uis():
    paths = {r.path for r in mod.api_router.routes}
    assert {"/api/webgate/capabilities", "/api/webgate/memory/search",
            "/api/webgate/flags"} <= paths
    assert mod.api_router in mod._API_ROUTERS()


def test_the_agent_readers_carry_no_mutation_guard():
    for route in mod.api_router.routes:
        if route.path in ("/api/webgate/capabilities", "/api/webgate/memory/search",
                          "/api/webgate/flags"):
            assert not route.dependencies, f"{route.path} guards a read"
            assert "GET" in (getattr(route, "methods", None) or set())


def test_tool_risk_uses_catalog_permissions():
    from webview.pages_new import _tools_section
    from core.tool_capabilities import CATALOG_ALIASES, high_risk_tool_ids, medium_risk_tool_ids
    high = {CATALOG_ALIASES.get(k, k) for k in high_risk_tool_ids()}
    medium = {CATALOG_ALIASES.get(k, k) for k in medium_risk_tool_ids()}
    for row in _tools_section()['items']:
        assert row['risk'] == ('high' if row['id'] in high else 'medium' if row['id'] in medium else 'low')


def test_mcp_inventory_uses_runtime_registration_gate(monkeypatch):
    monkeypatch.setenv('MCP_ENABLED', 'false')
    monkeypatch.setattr('tools.mcp.config.load_local_mcp_servers', lambda: {'local': {'command': 'example'}})
    body = mod._mcp_section()
    # Local server configuration enables registration even with the flag false,
    # exactly as the runtime bootstrap gate does; this is not connection health.
    assert body['enabled'] is True
    assert body['items'][0]['on'] is True
    monkeypatch.setattr('tools.mcp.config.load_local_mcp_servers', lambda: {})
    assert mod._mcp_section()['enabled'] is False
