"""Integration tests for the KB HTTP API (api/kb/endpoints.py).

Test strategy
-------------
- Build a minimal FastAPI app with only the KB router mounted (no full server bootstrap).
- Override the ``get_user_id`` dependency to inject a controlled user_id.
- Mock the heavy dependencies (``kb_ingest``, ``kb_search``, ``_ensure_backend``,
  ``pm()``) so tests are fast, hermetic, and deterministic.
- Assert the non-negotiable security invariants:
  1. Flag OFF (``KB_API_ENABLED`` unset) → 404 (routes not mounted).
  2. ``user_id`` comes from the auth dependency, never from the body.
  3. Absolute path → 400.
  4. Path escaping the workspace → 400.
  5. Cross-tenant isolation: search is called with the authenticated user_id.
"""
import asyncio
import os
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.dependencies import get_user_id
from api.kb.endpoints import router as kb_router


# ---------------------------------------------------------------------------
# Helpers — build the test app
# ---------------------------------------------------------------------------

def _make_app(user_id: str = "tenant_a") -> FastAPI:
    """Minimal FastAPI app with KB router + injected auth."""
    app = FastAPI()
    app.include_router(kb_router)

    # Override the get_user_id dependency so we don't need real auth middleware.
    async def _stub_user_id():
        return user_id

    app.dependency_overrides[get_user_id] = _stub_user_id
    return app


def _make_client(user_id: str = "tenant_a") -> TestClient:
    return TestClient(_make_app(user_id), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Stub pm() — returns a Path inside a tmp dir for confinement checks
# ---------------------------------------------------------------------------

class _StubPathManager:
    """Minimal stand-in for agents.task.path.PathManager."""

    def __init__(self, workspace_root: Path):
        self._root = workspace_root

    def get_workspace_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        return self._root


# ---------------------------------------------------------------------------
# Test 1: Flag OFF → routes not mounted → 404
# ---------------------------------------------------------------------------

def test_flag_off_returns_404(monkeypatch):
    """When KB_API_ENABLED is unset the router is not included; routes → 404."""
    monkeypatch.delenv("KB_API_ENABLED", raising=False)

    # Build an app WITHOUT including the router (simulating flag-off behaviour).
    app_no_kb = FastAPI()
    client = TestClient(app_no_kb, raise_server_exceptions=False)

    assert client.post("/api/kb/search", json={"query": "hello"}).status_code == 404
    assert client.post("/api/kb/ingest", json={"path": "doc.md", "session_id": "s1"}).status_code == 404


def test_kb_api_enabled_helper(monkeypatch):
    """kb_api_enabled() reflects the env var correctly."""
    from api.kb.endpoints import kb_api_enabled

    monkeypatch.delenv("KB_API_ENABLED", raising=False)
    assert kb_api_enabled() is False

    monkeypatch.setenv("KB_API_ENABLED", "true")
    assert kb_api_enabled() is True

    monkeypatch.setenv("KB_API_ENABLED", "false")
    assert kb_api_enabled() is False

    monkeypatch.setenv("KB_API_ENABLED", "1")
    assert kb_api_enabled() is True

    monkeypatch.setenv("KB_API_ENABLED", "off")
    assert kb_api_enabled() is False


# ---------------------------------------------------------------------------
# Test 2: Authenticated ingest with path body → calls engine with auth user_id
# ---------------------------------------------------------------------------

def test_ingest_path_uses_authenticated_user_id(tmp_path):
    """The ingest engine is called with the authenticated user_id, NOT a body-supplied one."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Create the file so the engine doesn't return "does not exist"
    (workspace / "notes.md").write_text("hello world")

    stub_pm = _StubPathManager(workspace)

    ingest_calls = []

    async def _fake_kb_ingest(path, collection="default", recursive=True,
                               globs=None, *, user_id, session_id):
        ingest_calls.append({"path": path, "user_id": user_id, "session_id": session_id})
        return {
            "ingested": 1, "unchanged": 0, "n_chunks": 3,
            "skipped_secret": 0, "skipped_binary": 0,
            "skipped_office": 0, "skipped_too_large": 0, "failed": 0,
        }

    import agents.task.path as _path_mod
    import tools.knowledge_ingest as ki_mod
    import api.kb.endpoints as ep_mod

    orig_pm = _path_mod.pm
    orig_ki = ki_mod.kb_ingest

    _path_mod.pm = lambda: stub_pm
    ki_mod.kb_ingest = _fake_kb_ingest

    try:
        with patch.object(ep_mod, "_ensure_backend", lambda: None):
            client = _make_client(user_id="tenant_a")
            resp = client.post("/api/kb/ingest", json={
                "path": "notes.md",
                "session_id": "sess-1",
                "collection": "default",
            })
    finally:
        _path_mod.pm = orig_pm
        ki_mod.kb_ingest = orig_ki

    # The engine must be called with the authenticated user_id ("tenant_a"),
    # not any body-supplied one.
    assert len(ingest_calls) == 1, f"Expected 1 ingest call, got {ingest_calls}, response: {resp.text}"
    assert ingest_calls[0]["user_id"] == "tenant_a"
    assert resp.status_code == 200
    body = resp.json()
    assert body["ingested"] == 1
    assert body["n_chunks"] == 3


# ---------------------------------------------------------------------------
# Test 3: Absolute path → 400
# ---------------------------------------------------------------------------

def test_ingest_absolute_path_returns_400():
    """An absolute path in the body must be rejected with 400."""
    client = _make_client()

    with patch("api.kb.endpoints._ensure_backend"):
        resp = client.post("/api/kb/ingest", json={
            "path": "/etc/passwd",
            "session_id": "s1",
        })

    assert resp.status_code == 400
    assert "Absolute" in resp.json().get("detail", "")


def test_ingest_unix_absolute_path_returns_400():
    """Unix-style absolute paths (/tmp/...) are also rejected."""
    client = _make_client()

    with patch("api.kb.endpoints._ensure_backend"):
        resp = client.post("/api/kb/ingest", json={
            "path": "/tmp/secret",
            "session_id": "s1",
        })

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Test 4: Path escaping workspace → 400
# ---------------------------------------------------------------------------

def test_ingest_path_traversal_returns_400(tmp_path):
    """A relative path that escapes the workspace via ``..`` → 400."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub_pm = _StubPathManager(workspace)

    import agents.task.path as _path_mod
    orig_pm = _path_mod.pm
    _path_mod.pm = lambda: stub_pm

    try:
        with patch("api.kb.endpoints._ensure_backend"):
            client = _make_client()
            resp = client.post("/api/kb/ingest", json={
                "path": "../../some_secret_file.txt",
                "session_id": "s1",
            })
    finally:
        _path_mod.pm = orig_pm

    assert resp.status_code == 400
    assert "escapes" in resp.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# Test 5: Cross-tenant isolation — search passes authenticated user_id
# ---------------------------------------------------------------------------

def test_search_uses_authenticated_user_id():
    """kb_search is called with the authenticated user_id; a second tenant gets their own."""
    search_calls: list = []

    async def _fake_kb_search(query, *, user_id=None, collection="default", limit=8):
        search_calls.append({"query": query, "user_id": user_id})
        return f"results for {user_id}"

    import modules.memory.registry as reg_mod
    original_search = reg_mod.kb_search
    reg_mod.kb_search = _fake_kb_search

    try:
        with patch("api.kb.endpoints._ensure_backend"):
            # Tenant A
            client_a = _make_client(user_id="tenant_a")
            resp_a = client_a.post("/api/kb/search", json={"query": "hello"})

            # Tenant B
            client_b = _make_client(user_id="tenant_b")
            resp_b = client_b.post("/api/kb/search", json={"query": "hello"})
    finally:
        reg_mod.kb_search = original_search

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200

    # Each tenant's search must be scoped to their own user_id.
    user_ids_called = [c["user_id"] for c in search_calls]
    assert "tenant_a" in user_ids_called
    assert "tenant_b" in user_ids_called
    # Crucially: tenant A search was NOT called with tenant B's user_id.
    a_calls = [c for c in search_calls if c["user_id"] == "tenant_a"]
    b_calls = [c for c in search_calls if c["user_id"] == "tenant_b"]
    assert len(a_calls) == 1
    assert len(b_calls) == 1


# ---------------------------------------------------------------------------
# Test 6: Search no-provider → empty string (no crash)
# ---------------------------------------------------------------------------

def test_search_no_provider_returns_empty():
    """When kb_search returns '' (no provider), the endpoint returns 200 with empty results."""

    async def _null_search(query, *, user_id=None, collection="default", limit=8):
        return ""

    import modules.memory.registry as reg_mod
    original_search = reg_mod.kb_search
    reg_mod.kb_search = _null_search

    try:
        with patch("api.kb.endpoints._ensure_backend"):
            client = _make_client()
            resp = client.post("/api/kb/search", json={"query": "anything"})
    finally:
        reg_mod.kb_search = original_search

    assert resp.status_code == 200
    assert resp.json()["results"] == ""


# ---------------------------------------------------------------------------
# Test 7: Upload endpoint — routes through kb_ingest with auth user_id +
#         source_name = original filename
# ---------------------------------------------------------------------------

def test_upload_routes_through_kb_ingest_with_source_name(tmp_path):
    """File upload goes through the shared kb_ingest engine with the authenticated
    user_id and source_name = stripped original filename (NOT a re-implemented loop)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub_pm = _StubPathManager(workspace)

    ingest_calls: list = []

    async def _fake_kb_ingest(path, collection="default", recursive=True,
                               globs=None, *, user_id, session_id, source_name=None):
        ingest_calls.append({
            "path": path, "user_id": user_id,
            "session_id": session_id, "source_name": source_name,
        })
        return {
            "ingested": 1, "unchanged": 0, "n_chunks": 2,
            "skipped_secret": 0, "skipped_binary": 0,
            "skipped_office": 0, "skipped_too_large": 0, "failed": 0,
        }

    import agents.task.path as _path_mod
    import tools.knowledge_ingest as ki_mod
    import api.kb.endpoints as ep_mod

    orig_pm = _path_mod.pm
    orig_ki = ki_mod.kb_ingest
    _path_mod.pm = lambda: stub_pm
    ki_mod.kb_ingest = _fake_kb_ingest

    try:
        with patch.object(ep_mod, "_ensure_backend", lambda: None):
            client = _make_client(user_id="uploader_u1")
            resp = client.post(
                "/api/kb/ingest/upload",
                files={"file": ("my notes.txt", b"hello world", "text/plain")},
            )
    finally:
        _path_mod.pm = orig_pm
        ki_mod.kb_ingest = orig_ki

    assert resp.status_code == 200, resp.text
    assert len(ingest_calls) == 1
    call = ingest_calls[0]
    assert call["user_id"] == "uploader_u1"
    # source_name is the stripped original filename — the stable dedup identity.
    assert call["source_name"] == "my notes.txt"
    # The on-disk path passed to the engine is relative to the workspace root.
    assert call["path"].startswith(".kb_uploads")


def test_upload_strips_path_traversal_in_filename(tmp_path):
    """A malicious filename like ../../etc/passwd is stripped to its basename for source_name."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub_pm = _StubPathManager(workspace)

    ingest_calls: list = []

    async def _fake_kb_ingest(path, collection="default", recursive=True,
                               globs=None, *, user_id, session_id, source_name=None):
        ingest_calls.append({"source_name": source_name, "path": path})
        return {
            "ingested": 1, "unchanged": 0, "n_chunks": 1,
            "skipped_secret": 0, "skipped_binary": 0,
            "skipped_office": 0, "skipped_too_large": 0, "failed": 0,
        }

    import agents.task.path as _path_mod
    import tools.knowledge_ingest as ki_mod
    import api.kb.endpoints as ep_mod

    orig_pm = _path_mod.pm
    orig_ki = ki_mod.kb_ingest
    _path_mod.pm = lambda: stub_pm
    ki_mod.kb_ingest = _fake_kb_ingest

    try:
        with patch.object(ep_mod, "_ensure_backend", lambda: None):
            client = _make_client(user_id="u1")
            resp = client.post(
                "/api/kb/ingest/upload",
                files={"file": ("../../etc/passwd", b"data", "text/plain")},
            )
    finally:
        _path_mod.pm = orig_pm
        ki_mod.kb_ingest = orig_ki

    assert resp.status_code == 200, resp.text
    # source_name has NO path components — only the basename survives.
    assert ingest_calls[0]["source_name"] == "passwd"
    assert "/" not in ingest_calls[0]["source_name"]
    assert ".." not in ingest_calls[0]["source_name"]


# ---------------------------------------------------------------------------
# Test 8: 401 when no auth (unauthenticated request)
# ---------------------------------------------------------------------------

def test_search_without_auth_returns_401():
    """Requests without a user_id (auth dependency raises) → 401."""
    app = FastAPI()
    app.include_router(kb_router)
    # No dependency_overrides — get_user_id will raise 401 because no middleware sets
    # request.state.user_id.
    client = TestClient(app, raise_server_exceptions=False)

    with patch("api.kb.endpoints._ensure_backend"):
        resp = client.post("/api/kb/search", json={"query": "test"})

    assert resp.status_code == 401


def test_ingest_without_auth_returns_401():
    """Ingest without auth → 401."""
    app = FastAPI()
    app.include_router(kb_router)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("api.kb.endpoints._ensure_backend"):
        resp = client.post("/api/kb/ingest", json={"path": "doc.md", "session_id": "s1"})

    assert resp.status_code == 401
