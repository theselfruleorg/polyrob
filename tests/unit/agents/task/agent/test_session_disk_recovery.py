"""Regression: SessionManager must rediscover sessions written in the FLAT layout
(data_root/<user>/<session>/metadata.json) that path.py actually writes."""
import json

from agents.task.agent.session import SessionManager


def _write_session(root, user, sid, status="created"):
    d = root / user / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps(
        {"id": sid, "user_id": user, "status": status}
    ))


def test_loads_flat_layout_sessions(tmp_path):
    _write_session(tmp_path, "usr_x", "sess_flat")          # NEW flat layout
    sm = SessionManager(base_dir=str(tmp_path))
    assert "sess_flat" in sm._sessions


def test_still_loads_legacy_layout(tmp_path):
    (tmp_path / "usr_y" / "sessions" / "sess_old").mkdir(parents=True)
    (tmp_path / "usr_y" / "sessions" / "sess_old" / "metadata.json").write_text(
        json.dumps({"id": "sess_old", "user_id": "usr_y", "status": "created"})
    )
    sm = SessionManager(base_dir=str(tmp_path))
    assert "sess_old" in sm._sessions


def test_running_marked_suspended_on_load(tmp_path):
    _write_session(tmp_path, "usr_x", "sess_run", status="running")
    sm = SessionManager(base_dir=str(tmp_path))
    assert sm._sessions["sess_run"]["status"] == "suspended"
