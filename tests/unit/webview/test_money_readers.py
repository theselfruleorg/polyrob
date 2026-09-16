"""Money readers: open bridges + on-chain creations (043 A37).

Two JSON readers on ``api_router`` (mounted in BOTH UIs, not the page switch).
What is tested here is what only these seams can get wrong: the origin-only
explorer link (an origin hash must NEVER be linked to the destination chain), the
honest states (an unreadable store is NAMED, never a confident empty), and that
each route scopes to the effective tenant.
"""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import webview.pages_new as mod
from core.status_snapshot import Section


# --- _bridges_body: origin-only links + honest states ----------------------- #

def test_a_base_origin_bridge_links_to_the_origin_explorer(monkeypatch):
    monkeypatch.setattr(
        "core.wallet.bridge_guard.open_bridges",
        lambda uid: [{
            "id": "b1", "state": "in_flight",
            "origin_chain_id": 8453, "dest_chain_id": 1,
            "currency_out": "ETH", "amount_usd": 12.5,
            "tx_ref": "0xdeadbeef", "created_at": time.time() - 60,
        }])
    body = mod._bridges_body("u1")
    assert body["unreadable"] is None
    row = body["bridges"][0]
    assert row["state"] == "in_flight"
    assert row["origin_chain"] == "base"
    assert row["dest_chain"] == "ethereum"
    # ⚠️ the link is the ORIGIN chain's explorer, never the destination.
    assert row["origin_link"] == "https://basescan.org/tx/0xdeadbeef"
    assert row["age_sec"] is not None and row["age_sec"] >= 0


def test_a_solana_or_unknown_origin_is_not_linked_to_the_wrong_chain(monkeypatch):
    """A Solana origin's stored id is the provider's pseudo id — it names no
    registry chain, so it MUST NOT be linked to the destination explorer."""
    monkeypatch.setattr(
        "core.wallet.bridge_guard.open_bridges",
        lambda uid: [{
            "id": "b2", "state": "pending",
            "origin_chain_id": 999999999, "dest_chain_id": 8453,
            "currency_out": "ETH", "amount_usd": 3.0,
            "tx_ref": "5xSolanaSig", "created_at": 0.0,
        }])
    row = mod._bridges_body("u1")["bridges"][0]
    assert row["origin_chain"] is None
    assert row["origin_link"] is None       # never the dest chain's explorer
    assert row["dest_chain"] == "base"


def test_an_unreadable_bridge_store_is_named_never_a_confident_empty(monkeypatch):
    def _boom(uid):
        raise RuntimeError("bridges.db locked")

    monkeypatch.setattr("core.wallet.bridge_guard.open_bridges", _boom)
    body = mod._bridges_body("u1")
    assert body["bridges"] is None
    assert "bridges.db locked" in body["unreadable"]


def test_a_genuinely_empty_board_is_an_empty_list(monkeypatch):
    monkeypatch.setattr("core.wallet.bridge_guard.open_bridges", lambda uid: [])
    body = mod._bridges_body("u1")
    assert body == {"bridges": [], "unreadable": None}


# --- _creations_body: reuses the snapshot section --------------------------- #

def test_creations_reuses_the_snapshot_section(monkeypatch):
    sec = Section(name="creations")
    sec.data["creations"] = [{"action": "deploy_token", "address": "0xabc",
                              "chain": "base", "url": "https://basescan.org/token/0xabc"}]
    sec.data["unreadable_rows"] = 0
    monkeypatch.setattr("core.status_snapshot._creations_section",
                        lambda uid, data_dir: sec)
    body = mod._creations_body("u1")
    assert body["state"] == "ok"
    assert body["creations"][0]["url"] == "https://basescan.org/token/0xabc"
    assert body["unreadable_rows"] == 0


def test_an_unreadable_creations_store_is_unavailable_not_empty(monkeypatch):
    def _boom(uid, data_dir):
        raise FileNotFoundError("telemetry_events.db not found")

    monkeypatch.setattr("core.status_snapshot._creations_section", _boom)
    body = mod._creations_body("u1")
    assert body["state"] == "unavailable"
    assert body["creations"] is None        # never a confident empty list
    assert "telemetry_events.db" in (body["reason"] or "")


# --- the endpoints ---------------------------------------------------------- #

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("webview.pages._effective_user_id", lambda request: "u1")
    app = FastAPI()
    app.include_router(mod.api_router)
    return TestClient(app)


def test_the_bridges_endpoint_scopes_to_the_tenant(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(mod, "_bridges_body",
                        lambda uid: seen.update(uid=uid) or {"bridges": [], "unreadable": None})
    resp = client.get("/api/webgate/bridges")
    assert resp.status_code == 200
    assert resp.json() == {"bridges": [], "unreadable": None}
    assert seen == {"uid": "u1"}


def test_the_creations_endpoint_scopes_to_the_tenant(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(mod, "_creations_body",
                        lambda uid: seen.update(uid=uid) or {"state": "ok",
                                                             "creations": [],
                                                             "reason": None,
                                                             "unreadable_rows": 0})
    resp = client.get("/api/webgate/creations")
    assert resp.status_code == 200
    assert resp.json()["state"] == "ok"
    assert seen == {"uid": "u1"}


def test_both_readers_are_gets_with_no_mutation_guard():
    """Reads never carry the write guard; they ride ``api_router`` (both UIs)."""
    paths = {}
    for route in mod.api_router.routes:
        methods = getattr(route, "methods", None) or set()
        if getattr(route, "path", "") in ("/api/webgate/bridges",
                                          "/api/webgate/creations"):
            paths[route.path] = methods
            assert not route.dependencies, f"{route.path} guards a read"
    assert set(paths) == {"/api/webgate/bridges", "/api/webgate/creations"}
    assert all("GET" in m for m in paths.values())


def test_the_readers_mount_in_both_uis():
    """``api_router`` is returned by ``_API_ROUTERS`` — the seam that mounts
    endpoints regardless of ``WEBVIEW_UI``."""
    assert mod.api_router in mod._API_ROUTERS()
