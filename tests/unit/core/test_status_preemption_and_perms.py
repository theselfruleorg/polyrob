"""057 WS-C / WS-G renders on the ONE snapshot: pre-emption counts, a repeated
rail cut names the cap it needs, and the data-home permission audit is a
security line that is never silently ok."""
import os
import time

from core.status_snapshot import SEVERITY_WARN, build_status_snapshot
from tests.unit.core.test_status_snapshot import OWNER, _seed_cron, _seed_goals


def _env(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setenv("POLYROB_DATA_DIR", d)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", os.path.join(d, "telemetry_events.db"))
    monkeypatch.setenv("GOALS_ENABLED", "false")
    monkeypatch.setenv("CRON_ENABLED", "false")
    monkeypatch.delenv("DB_PATH", raising=False)
    _seed_goals(d, ready=1, blocked=False, asks=False, exhausted_objective=False)
    _seed_cron(d)
    from core.event_log import TelemetryEventLog
    return d, TelemetryEventLog(os.path.join(d, "telemetry_events.db"))


def test_yields_render_and_unresumed_yields_warn(tmp_path, monkeypatch):
    d, log = _env(tmp_path, monkeypatch)
    now = time.time()
    for i in range(4):
        log.record("goal_run", user_id=OWNER, source="goal", session_id="s1",
                   attrs={"outcome": "yielded", "goal_id": "g1", "reason": "job1", "step": 8},
                   ts=now - 100 - i)
    log.record("goal_run", user_id=OWNER, source="goal",
               attrs={"outcome": "deferred", "reason": "headroom:job1", "ready": 2}, ts=now - 50)
    log.record("goal_run", user_id=OWNER, source="goal",
               attrs={"outcome": "deferred", "reason": "owner_turn"}, ts=now - 40)
    snap = build_status_snapshot(OWNER, data_dir=d, include_money=False)
    work = snap.sections["work"]
    line = next(l for l in work.lines if l.startswith("pre-emption"))
    assert "yielded ×4" in line and "resumed ×0" in line and "deferred ×2" in line
    assert "headroom 1" in line and "owner_turn 1" in line and "goal g1 yielded 4×" in line
    assert work.data["preemption"]["per_goal"] == {"g1": 4}
    item = next(h for h in snap.health if h.key == "yields_not_resumed")
    assert item.severity == SEVERITY_WARN and "GOAL_RESUME_SAME_SESSION" in item.remedy
    # one resume clears the warning
    log.record("goal_run", user_id=OWNER, source="goal", session_id="s1",
               attrs={"outcome": "resumed", "goal_id": "g1"}, ts=now - 10)
    snap = build_status_snapshot(OWNER, data_dir=d, include_money=False)
    assert not any(h.key == "yields_not_resumed" for h in snap.health)
    assert "resumed ×1" in next(l for l in snap.sections["work"].lines if l.startswith("pre-emption"))


def test_a_rail_cut_twice_names_the_cap_it_needs(tmp_path, monkeypatch):
    d, log = _env(tmp_path, monkeypatch)
    now = time.time()
    log.record("cron_run", user_id=OWNER, source="cron",
               attrs={"job_id": "job1", "outcome": "done", "duration_s": 700.0, "steps": 9}, ts=now - 9000)
    for i in range(2):
        log.record("cron_run", user_id=OWNER, source="cron",
                   attrs={"job_id": "job1", "outcome": "started"}, ts=now - 3000 - i * 1000)
        log.record("cron_run", user_id=OWNER, source="cron",
                   attrs={"job_id": "job1", "outcome": "cut_by_cap", "reason": "600s",
                          "cap_s": 600, "duration_s": 600.0}, ts=now - 2900 - i * 1000)
    snap = build_status_snapshot(OWNER, data_dir=d, include_money=False)
    item = next(h for h in snap.health if h.key == "rail_cut_repeat")
    assert "cut 2× in 24h" in item.text and "longest completed run 11m" in item.text
    assert "--max-duration" in item.remedy
    assert any(h.key == "rail_cut" for h in snap.health)          # the single-cut WARN still fires


def test_one_cut_is_not_a_repeat(tmp_path, monkeypatch):
    d, log = _env(tmp_path, monkeypatch)
    now = time.time()
    log.record("cron_run", user_id=OWNER, source="cron",
               attrs={"job_id": "job1", "outcome": "cut_by_cap", "reason": "600s"}, ts=now - 100)
    snap = build_status_snapshot(OWNER, data_dir=d, include_money=False)
    assert not any(h.key == "rail_cut_repeat" for h in snap.health)


def test_data_perms_is_a_security_line_and_never_silently_ok(tmp_path, monkeypatch):
    d, _ = _env(tmp_path, monkeypatch)
    snap = build_status_snapshot(OWNER, data_dir=d, include_money=False)
    sec = snap.sections["security"]
    assert "data_perms" in sec.data
    perms = sec.data["data_perms"]
    line = next(l for l in sec.lines if "perm" in l.lower() or "not checked" in l.lower())
    if perms["skipped"]:
        assert "not checked" in line                     # a dev box without the group says so
    else:
        assert perms["scanned"] > 0
