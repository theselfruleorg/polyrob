"""H07 — the root supervisor trusts no registry field and no path under the data home."""
import asyncio
import http.server
import os
import threading

import pytest

from core.app_service import owner_ops
from core.app_service.registry import AppServiceRegistry
from core.app_service.supervisor import _default_http_probe
from core.sqlite_util import execute_retry

from tests.unit.core.app_service.test_app_supervisor import rig  # noqa: F401  (fixture)


def _req(r, slug="st", uid="u1", **over):
    kw = dict(source_dir="/tmp/x", cmd=["python", "app.py"], container_port=8765,
              health_path="/", egress="none", egress_allow=[], env={},
              workspace_digest="a" * 64)
    kw.update(over)
    return r.upsert_request(slug, uid, **kw)


@pytest.mark.parametrize("over,needle", [
    (dict(slug="../../etc"), "slug"),
    (dict(slug="www"), "slug"),
    (dict(uid="a/b"), "tenant"),
    (dict(uid=".."), "tenant"),
    (dict(workspace_digest="/etc"), "workspace_digest"),
    (dict(workspace_digest="abc"), "workspace_digest"),
    (dict(workspace_digest="A" * 64), "workspace_digest"),
])
def test_registry_refuses_unsafe_row_fields(tmp_path, over, needle):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    with pytest.raises(ValueError, match=needle):
        _req(r, **over)


def _sql(reg, sql, args):
    execute_retry(reg.db_path, sql, args)


def test_forged_approval_is_sent_back_to_pending(rig):  # noqa: F811
    # A db writer (any polyrob-data identity) flips a PENDING row to approved.
    _sql(rig.reg, "UPDATE app_services SET status='pending', approval_mac=NULL "
                  "WHERE slug='rob-status'", ())
    _sql(rig.reg, "UPDATE app_services SET status='approved' WHERE slug='rob-status'", ())
    asyncio.run(rig.sup.tick())
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "pending"
    assert "not signed" in row["last_failure_error"]
    assert rig.docker.argv_of(["run", "-d"]) == []


def test_config_moved_under_a_signed_approval_is_refused(rig):  # noqa: F811
    _sql(rig.reg, "UPDATE app_services SET cmd=? WHERE slug='rob-status'",
         ('["sh", "-c", "evil"]',))
    asyncio.run(rig.sup.tick())
    assert rig.reg.get("rob-status", "owner-1")["status"] == "pending"
    assert rig.docker.argv_of(["run", "-d"]) == []


def test_tampered_digest_never_reaches_the_filesystem(rig, tmp_path):  # noqa: F811
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    _sql(rig.reg, "UPDATE app_services SET workspace_digest=? WHERE slug='rob-status'",
         (str(victim),))
    asyncio.run(rig.sup.tick())
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "failed" and "refused by supervisor" in row["last_failure_error"]
    assert (victim / "keep").read_text() == "x"
    assert rig.docker.argv_of(["run", "-d"]) == []


def test_symlinked_apps_tree_is_refused_not_followed(rig, tmp_path):  # noqa: F811
    outside = tmp_path / "outside"
    outside.mkdir()
    apps = rig.tmp / "data" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    os.symlink(str(outside), str(apps / "owner-1"))
    asyncio.run(rig.sup.tick())
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "failed"
    assert list(outside.iterdir()) == []


def test_apps_dirs_lose_group_write(rig):  # noqa: F811
    asyncio.run(rig.sup.tick())
    assert rig.reg.get("rob-status", "owner-1")["status"] == "live"
    for p in ("apps", "apps/owner-1", "apps/owner-1/rob-status",
              "apps/owner-1/rob-status/snapshots"):
        assert not (os.stat(rig.tmp / "data" / p).st_mode & 0o022), p


def test_logs_refresh_does_not_follow_a_planted_symlink(rig, tmp_path):  # noqa: F811
    asyncio.run(rig.sup.tick())
    assert rig.reg.get("rob-status", "owner-1")["status"] == "live"
    target = tmp_path / "shadow"
    target.write_text("root-owned secret\n")
    logs = rig.tmp / "data" / "apps" / "owner-1" / "rob-status" / "logs.txt"
    if logs.exists() or logs.is_symlink():
        logs.unlink()
    os.symlink(str(target), str(logs))
    asyncio.run(rig.sup.tick())
    assert target.read_text() == "root-owned secret\n"
    assert not logs.is_symlink() and "hello from app" in logs.read_text()


def test_row_container_name_is_never_used(rig):  # noqa: F811
    asyncio.run(rig.sup.tick())
    _sql(rig.reg, "UPDATE app_services SET container_name='polyrob-sandbox-victim', "
                  "status='stopped' WHERE slug='rob-status'", ())
    asyncio.run(rig.sup.tick())
    rms = [a for a in rig.docker.log if a[:2] == ["rm", "-f"]]
    assert rms and all(a[2] == "polyrob-app-owner-1-rob-status" for a in rms)


def test_owner_seat_without_the_key_cannot_approve(tmp_path):
    key = tmp_path / "k"
    os.symlink("/dev/null", str(key))  # a planted link is refused, never read
    r = AppServiceRegistry(str(tmp_path / "a.db"), approval_key_path=str(key))
    _req(r)
    ok, msg = owner_ops.approve(r, "st", "u1", via="test")
    assert not ok and "cannot sign" in msg
    assert r.get("st", "u1")["status"] == "pending"


def test_approval_key_is_private(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "a.db"))
    _req(r)
    assert r.mark_approved("st", "u1")
    st = os.stat(r.approval_key_path)
    assert (st.st_mode & 0o077) == 0
    assert r.verify_approval(r.get("st", "u1")) is None


def test_health_probe_does_not_follow_redirects():
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            if self.path == "/health":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        ok = asyncio.run(_default_http_probe(f"http://127.0.0.1:{port}/health", 5.0))
    finally:
        srv.shutdown()
    assert ok is False
    assert hits == ["/health"]
