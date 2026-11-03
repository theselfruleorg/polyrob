"""098 MO-10: cron payload writers change only their own key."""
from datetime import datetime

from cron.jobs import CronJob, CronJobStore


def _store(tmp_path):
    s = CronJobStore(str(tmp_path / "cron.db"))
    s.add(CronJob(id="j", task="t", schedule_spec="1h", user_id="u", payload={"k": 1},
                      next_run_at=datetime(2026, 1, 1)))
    return s


def test_set_priority_keeps_concurrent_last_outcome(tmp_path, monkeypatch):
    s = _store(tmp_path)
    stale = s.get("j")
    s.record_outcome("j", "ok", datetime(2026, 1, 1))
    monkeypatch.setattr(s, "get", lambda *a, **k: stale)
    assert s.set_priority("j", "money")
    monkeypatch.undo()
    p = s.get("j").payload
    assert p["priority"] == "money" and p["last_outcome"]["outcome"] == "ok" and p["k"] == 1


def test_set_preempts_keeps_paused_note(tmp_path):
    s = _store(tmp_path)
    assert s.pause("j", user_id="u", note={"by": "owner"})
    assert s.set_preempts("j", True, user_id="u")
    p = s.get("j").payload
    assert p["preempts"] is True and p["paused"] == {"by": "owner"}
    assert s.set_preempts("j", False, user_id="other") is False
    assert s.set_priority("j", "bogus") is False
