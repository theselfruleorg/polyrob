"""RC-2 (2026-07-07): the own_ops/local owner sees ALL sessions in the catalog.

Real sessions are tagged with OTHER user_ids than the owner-login identity
(CLI sessions are user_id="local", telegram principals are "u_<hash>", …).
_check_session_ownership already grants the own_ops owner every session; the
CATALOG listed only the login identity's own dir.

⚠️ 043 A12 moved the catalog. ``GET /api/sessions`` and its four readers
(``_collect_sessions_in_dir`` / ``_finalize_session_rows`` /
``_get_user_sessions`` / ``_get_all_sessions``) are DELETED — that route walked
every session directory on the event loop for a page that no longer exists. The
live catalog is ``GET /api/webgate/chats``, which pairs the SAME security rule
(``server._catalog_scope``) with ``webview/session_catalog.py::session_page``,
one hydrated page at a time. So these tests pin the rule where it now runs.

Scope rules pinned here:
  - local:      the loopback operator IS the owner → all user dirs.
  - own_ops:    authenticated owner-login identity → all user dirs;
                any other identity (or no auth) → [].
  - multitenant: strictly per-tenant, byte-identical to before.
"""
import asyncio
import json

import pytest


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self, user_id=None, authenticated=False):
        self.state = _FakeState()
        self.headers = {}
        self.cookies = {}
        if user_id is not None:
            self.state.user_id = user_id
        self.state.authenticated = authenticated


def _seed_session(root, user_id, session_id, task="do a thing", creator=None):
    sess = root / user_id / session_id
    (sess / "feed").mkdir(parents=True)
    payload = {"task": task, "model": "m1", "provider": "p1"}
    if creator is not None:
        payload["creator"] = creator
    (sess / "task.json").write_text(json.dumps(payload))
    # get_session_user discovery requires valid metadata (path.py PRIORITY 1)
    (sess / "metadata.json").write_text(json.dumps(
        {"user_id": user_id, "task": task}))


@pytest.fixture
def catalog_tree(tmp_path):
    """Three user dirs, like the real prod tree: owner-login, CLI, telegram."""
    root = tmp_path / "sessions"
    _seed_session(root, "rob", "s-console-1")
    _seed_session(root, "local", "s-cli-1")
    _seed_session(root, "u_abc123", "s-tg-1")
    from agents.task.path import get_path_manager, set_path_manager, reset_path_manager
    set_path_manager(get_path_manager(data_root=str(root)))
    yield root
    reset_path_manager()


@pytest.fixture(autouse=True)
def _posture_env(monkeypatch):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL_OWNER", raising=False)
    yield


def _api_sessions(server, request):
    """The catalog rows a request may see — the live ``/api/webgate/chats`` path.

    ``_catalog_scope`` (the security rule) + ``session_page`` (the reader) +
    ``_annotate_runtime`` (where a live session runs), exactly as
    ``pages_new.api_chats`` composes them.
    """
    from webview.session_catalog import session_page
    scope, user_id = server._catalog_scope(request)
    body = session_page(server.pm().data_root, scope, user_id)
    return server._annotate_runtime(body["sessions"])


def test_local_owner_sees_all_user_dirs(monkeypatch, catalog_tree):
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    import webview.server as server
    rows = _api_sessions(server, _FakeRequest())
    assert {r["id"] for r in rows} == {"s-console-1", "s-cli-1", "s-tg-1"}
    assert {r["user"] for r in rows} == {"rob", "local", "u_abc123"}


def test_own_ops_owner_sees_all_user_dirs(monkeypatch, catalog_tree):
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    import webview.server as server
    owner = server.webgate.local_owner_id()
    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: True)
    rows = _api_sessions(server, _FakeRequest(user_id=owner, authenticated=True))
    assert {r["id"] for r in rows} == {"s-console-1", "s-cli-1", "s-tg-1"}


def test_own_ops_non_owner_identity_gets_empty(monkeypatch, catalog_tree):
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    import webview.server as server
    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: True)
    rows = _api_sessions(server, _FakeRequest(user_id="mallory", authenticated=True))
    assert rows == []


def test_own_ops_unauthenticated_gets_empty(monkeypatch, catalog_tree):
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    import webview.server as server
    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: False)
    rows = _api_sessions(server, _FakeRequest())
    assert rows == []


def test_multitenant_stays_strictly_per_tenant(monkeypatch, catalog_tree):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    import webview.server as server
    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: True)
    rows = _api_sessions(server, _FakeRequest(user_id="u_abc123", authenticated=True))
    assert {r["id"] for r in rows} == {"s-tg-1"}


def test_multitenant_unauthenticated_still_empty(monkeypatch, catalog_tree):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    import webview.server as server
    import utils.auth_utils as au
    monkeypatch.setattr(au, "is_authenticated", lambda request: False)
    rows = _api_sessions(server, _FakeRequest())
    assert rows == []


def test_runtime_annotation_marks_remote_sessions(monkeypatch, catalog_tree):
    """WS-4 minimum: an active-looking session owned by ANOTHER process (the
    agent service, via the P6 sqlite registry) is labeled runtime='agent' with
    its owner_pid; one resident here is 'here'; inactive rows untouched."""
    from unittest.mock import MagicMock
    from agents.task.session_route import SessionRoute, LOCAL, REMOTE

    monkeypatch.setenv("POLYROB_POSTURE", "local")
    import webview.server as server

    (catalog_tree / "local" / "s-cli-1" / "status.json").write_text(
        json.dumps({"status": "running"}))
    (catalog_tree / "rob" / "s-console-1" / "status.json").write_text(
        json.dumps({"status": "running"}))

    routes = {
        "s-cli-1": SessionRoute(status=REMOTE, owner_pid=4242),
        "s-console-1": SessionRoute(status=LOCAL, owner_pid=1),
    }
    fake_agent = MagicMock()
    fake_agent.route_session = lambda sid: routes.get(sid)
    monkeypatch.setattr(server, "_in_process_task_agent", lambda: fake_agent)

    rows = {r["id"]: r for r in _api_sessions(server, _FakeRequest())}
    assert rows["s-cli-1"]["runtime"] == "agent"
    assert rows["s-cli-1"]["owner_pid"] == 4242
    assert rows["s-console-1"]["runtime"] == "here"
    assert "runtime" not in rows["s-tg-1"]  # completed → not queried


def test_runtime_annotation_absent_without_agent(monkeypatch, catalog_tree):
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    import webview.server as server
    monkeypatch.setattr(server, "_in_process_task_agent", lambda: None)
    rows = _api_sessions(server, _FakeRequest())
    assert all("runtime" not in r for r in rows)


def test_own_ops_owner_reads_status_of_cli_owned_session(monkeypatch, catalog_tree):
    """2.3 regression: per-session READ endpoints resolve the session's OWN
    user via pm().get_session_user, so the own_ops owner can read a session
    whose metadata user_id is 'local' (CLI-created) once pm() points at the
    real tree."""
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    import webview.server as server
    owner = server.webgate.local_owner_id()
    import utils.auth_utils as au
    monkeypatch.setattr(au, "get_authenticated_user_id", lambda request: owner)
    # status.json for the CLI session
    (catalog_tree / "local" / "s-cli-1" / "status.json").write_text(
        json.dumps({"status": "completed"}))

    resp = asyncio.run(server.api_session_status(
        _FakeRequest(user_id=owner, authenticated=True), "s-cli-1"))
    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert body["status"] == "completed"


def test_per_tenant_scope_reads_only_that_tenants_dir(monkeypatch, catalog_tree):
    """The multitenant contract: only the named user's rows, and an UNKNOWN
    status stays unknown rather than being invented."""
    from webview.session_catalog import session_page
    import webview.server as server
    body = session_page(server.pm().data_root, "user", "local")
    assert [r["id"] for r in body["sessions"]] == ["s-cli-1"]
    assert body["sessions"][0]["user"] == "local"
    assert body["sessions"][0]["status"] is None  # no status.json written


def test_catalog_row_carries_the_creator_label(monkeypatch, catalog_tree):
    """A17: the `creator` recorded at session creation survives into the row —
    so the catalog can say a run came from cron rather than from a person."""
    from webview.session_catalog import session_page
    import webview.server as server
    task_file = catalog_tree / "local" / "s-cli-1" / "task.json"
    payload = json.loads(task_file.read_text())
    payload["creator"] = "cron"
    task_file.write_text(json.dumps(payload))

    body = session_page(server.pm().data_root, "user", "local")
    assert body["sessions"][0]["creator"] == "cron"


def test_the_deleted_catalog_route_and_readers_are_gone():
    """043 A12: the loop-blocking full-tree catalog must not come back."""
    import webview.server as server
    for name in ("api_sessions", "_sessions_for_request", "_get_all_sessions",
                 "_get_user_sessions", "_collect_sessions_in_dir",
                 "_finalize_session_rows"):
        assert not hasattr(server, name), f"{name} is back on webview.server"
    paths = {getattr(r, "path", "") for r in server._fastapi.router.routes}
    assert "/api/sessions" not in paths
