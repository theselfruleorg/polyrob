"""043 A16 — GET /api/webgate/artifacts: the session's files, typed.

Read-only, tenant-scoped over ``ArtifactLedger.list_for_session``. A session
with recorded artifacts lists each with its kind + verdict; an unbound own_ops
console 403s (via ``_effective_user_id``); a session with nothing recorded is an
empty list, not an error.

The router is mounted alone on a fresh app (the inbox-test pattern) so the
console's own posture is what the endpoint sees, not the full server's auth
middleware.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_ambient_owner(monkeypatch):
    """The dev box may carry a real owner in its environment."""
    for name in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                 "POLYROB_LOCAL_OWNER", "SURFACE_SUPER_ADMIN_USER_IDS"):
        monkeypatch.delenv(name, raising=False)


def _client(monkeypatch, posture="local"):
    import webview.webgate as webgate
    import webview.artifacts_api as mod
    monkeypatch.setattr(webgate, "posture", lambda: posture)
    monkeypatch.setattr(webgate, "read_only", lambda: True)  # silence unbound warn
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def _ledger_on(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_DB_PATH", str(tmp_path / "artifacts.db"))
    import core.artifacts as artmod
    artmod.reset_artifact_ledger()
    return artmod


@pytest.fixture(autouse=True)
def _reset_ledger():
    yield
    import core.artifacts as artmod
    artmod.reset_artifact_ledger()


def test_lists_recorded_artifacts_with_kind_and_verdict(monkeypatch, tmp_path):
    artmod = _ledger_on(tmp_path, monkeypatch)
    import webview.webgate as webgate
    owner = webgate.local_owner_id()  # "local" — the single-user tenant

    import agents.task.path as pth
    clean = pth.pm().clean_session_id("sess-art")

    ledger = artmod.get_artifact_ledger()
    py = tmp_path / "app.py"
    py.write_text("print(1)\n")
    csv = tmp_path / "data.csv"
    csv.write_text("a,b\n1,2\n")
    a1 = ledger.record(owner, str(py), session_id=clean, kind=artmod.kind_for_path(str(py)))
    a2 = ledger.record(owner, str(csv), session_id=clean, kind=artmod.kind_for_path(str(csv)))
    assert a1 is not None and a2 is not None

    client = _client(monkeypatch, posture="local")
    resp = client.get("/api/webgate/artifacts?session_id=sess-art")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"] is None
    kinds = {a["path"]: a["kind"] for a in body["artifacts"]}
    assert kinds == {"app.py": artmod.KIND_CODE, "data.csv": artmod.KIND_DATA}
    for a in body["artifacts"]:
        assert a["verdict"] == "ok"  # files still on disk, hash matches
        assert a["id"]


def test_unbound_own_ops_console_403s(monkeypatch, tmp_path):
    _ledger_on(tmp_path, monkeypatch)
    client = _client(monkeypatch, posture="own_ops")
    resp = client.get("/api/webgate/artifacts?session_id=whatever")
    assert resp.status_code == 403, resp.text


def test_missing_session_is_empty_list_no_error(monkeypatch, tmp_path):
    _ledger_on(tmp_path, monkeypatch)
    client = _client(monkeypatch, posture="local")
    resp = client.get("/api/webgate/artifacts?session_id=never-ran")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"artifacts": [], "error": None}


def test_no_session_id_is_a_refusal_never_an_empty_list(monkeypatch, tmp_path):
    """043 A3: this ledger is SESSION-scoped, so a call without one is refused.

    It used to answer ``{"artifacts": [], "error": None}`` — the sentence "this
    produced nothing" — and Work › Apps, which calls it with no session id,
    drew "Nothing built" over a tree full of real files.
    """
    _ledger_on(tmp_path, monkeypatch)
    client = _client(monkeypatch, posture="local")
    resp = client.get("/api/webgate/artifacts")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"artifacts": None, "error": "session-scoped"}


def test_router_exposes_the_artifacts_path():
    """The router carries the GET route the server mounts into both UIs."""
    import webview.artifacts_api as mod
    routes = [r for r in mod.router.routes if getattr(r, "path", None) == "/api/webgate/artifacts"]
    assert routes, "artifacts route missing from the router"
    assert "GET" in routes[0].methods
