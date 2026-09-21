from core.app_service.owner_ops import APPS_EMPTY_LINE
"""032 — the owner verbs and the status-snapshot section every seat renders from."""
import os

from core.app_service import owner_ops
from core.app_service.registry import AppServiceRegistry


def _reg(tmp_path):
    r = AppServiceRegistry(str(tmp_path / "app_services.db"))
    r.upsert_request("st", "u1", source_dir="/p/st", cmd=["python", "server.py"], container_port=8765,
                     health_path="/", egress="none", egress_allow=[], env={"LOG": "x"},
                     workspace_digest="d" * 64)
    return r


def test_approve_reject_kill_and_lines(tmp_path):
    r = _reg(tmp_path)
    lines = owner_ops.list_lines(r, "u1")
    assert lines[0] == "1 app(s):" and "st [pending] pending your approval" in lines[1]
    assert lines[-1].startswith("approve:")
    ok, msg = owner_ops.reject(r, "nope", "u1", via="cli")
    assert not ok and "no app" in msg
    ok, msg = owner_ops.kill(r, "st", "u1", via="cli")   # kill works on pending too
    assert ok and r.get("st", "u1")["status"] == "stopped"
    r2 = _reg(tmp_path / "b")
    ok, msg = owner_ops.approve(r2, "st", "u1", via="telegram")
    assert ok and "approved" in msg and r2.get("st", "u1")["status"] == "approved"
    ok, msg = owner_ops.approve(r2, "st", "u1", via="telegram")
    assert not ok and "not pending" in msg
    ok, msg = owner_ops.reject(r2, "st", "u1", via="cli")
    assert not ok and "kill" in msg
    r2.record_live("st", "u1", host_port=18000, container_name="c", public_url="https://st.apps.x")
    show = owner_ops.show_lines(r2, "u1", "st")
    assert show[0] == "st [live] https://st.apps.x" and "cmd python server.py" in show[2]
    ok, msg = owner_ops.kill(r2, "st", "u1", via="webview")
    assert ok and "stop queued" in msg and r2.get("st", "u1")["last_failure_error"] == "killed by owner via webview"
    ok, msg = owner_ops.kill(r2, "st", "u1", via="webview")
    assert ok and "already stopped" in msg
    r3 = _reg(tmp_path / "c")
    ok, msg = owner_ops.reject(r3, "st", "u1", via="cli")
    assert ok and r3.get("st", "u1")["status"] == "stopped"
    assert owner_ops.row_json(r3.get("st", "u1"))["where"] == "not running"
    assert owner_ops.list_lines(r3, "nobody") == [owner_ops.APPS_EMPTY_LINE]


def test_logs_tail(tmp_path):
    assert "no logs yet" in owner_ops.logs_tail(str(tmp_path), "u1", "st")
    from core.app_service.config import logs_path
    p = logs_path(str(tmp_path), "u1", "st")
    os.makedirs(os.path.dirname(p))
    open(p, "w").write("a\nb\nc\n")
    assert owner_ops.logs_tail(str(tmp_path), "u1", "st", 2) == "b\nc\n"


def test_status_snapshot_apps_section(tmp_path, monkeypatch):
    from core.status_snapshot import SECTION_ORDER, build_status_snapshot
    from core.status_render import render_section_lines
    assert "apps" in SECTION_ORDER
    monkeypatch.setenv("APP_SERVICES_DB_PATH", str(tmp_path / "app_services.db"))
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert snap.section("apps").lines == ["no apps"] and snap.section("apps").available
    r = _reg(tmp_path)
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    sec = snap.section("apps")
    assert sec.lines[0] == "1 pending" and any("st: pending" in ln for ln in sec.lines)
    assert [h.key for h in sec.health] == ["apps_pending"]
    assert any(h.key == "apps_pending" and h.severity == "crit" for h in snap.health)
    assert render_section_lines(snap, "apps")[0].startswith("• Apps: 1 pending")
    r.mark_approved("st", "u1")
    r.record_failed("st", "u1", error="health check failed")
    snap = build_status_snapshot("u1", data_dir=str(tmp_path), include_money=False)
    assert [h.key for h in snap.section("apps").health] == ["apps_failed"]
    snap = build_status_snapshot("", data_dir=str(tmp_path), include_money=False)
    assert not snap.section("apps").available


def test_every_owner_seat_says_WHY_a_re_approval_is_pending(tmp_path):
    """The owner must be able to read which fields moved since they approved —
    the list, the show block and the console JSON all render one helper."""
    r = _reg(tmp_path)
    assert r.mark_approved("st", "u1")
    r.upsert_request("st", "u1", source_dir="/p/st", cmd=["python", "server.py"],
                     container_port=8765, health_path="/", egress="open", egress_allow=[],
                     env={"LOG": "x"}, workspace_digest="e" * 64)
    row = r.get("st", "u1")
    assert row["status"] == "pending"
    assert owner_ops.pending_reason(row) == "the egress mode changed"
    assert "the egress mode changed; re-approval needed" in "\n".join(owner_ops.list_lines(r, "u1"))
    assert "re-approval needed" in owner_ops.show_lines(r, "u1", "st")[0]
    j = owner_ops.row_json(row)
    assert j["pending_reason"] == "the egress mode changed" and j["approval_change"] == ["egress"]
    # a FIRST approval is not a "change"
    fresh = _reg(tmp_path / "c")
    assert owner_ops.pending_reason(fresh.get("st", "u1")) is None
