"""Work (2026-11-20 browser walk): Stop names the job, one remedy for "autonomy
off", a ready goal with autonomy off waits for autonomy, and a lane's count sits
beside its own title."""
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webview.copy import STRINGS, t

_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _no_ambient_owner(monkeypatch):
    for name in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                 "POLYROB_LOCAL_OWNER", "SURFACE_SUPER_ADMIN_USER_IDS"):
        monkeypatch.delenv(name, raising=False)


# --- task 3: Stop names the job ---------------------------------------------- #

def _cron_client(monkeypatch, tmp_path):
    import webview.job_routes as jr
    import webview.pages as pages
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "posture", lambda: "local")
    monkeypatch.setattr(webgate, "read_only", lambda: False)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.cron_db_path",
                        lambda _d=None: str(tmp_path / "cron.db"))
    app = FastAPI()
    app.include_router(jr.router)
    app.dependency_overrides = {}
    for dep in webgate.MUTATION_DEPS:
        app.dependency_overrides[dep.dependency] = lambda: None
    return TestClient(app)


def test_stop_answers_with_the_jobs_name_never_its_id(monkeypatch, tmp_path):
    from cron.jobs import CronJobStore
    from cron.service import CronService
    import webview.webgate as webgate
    svc = CronService(CronJobStore(str(tmp_path / "cron.db")))
    job = svc.schedule(task="Summarise the week\nwith detail", schedule_spec="30m",
                       user_id=webgate.local_owner_id())
    client = _cron_client(monkeypatch, tmp_path)
    r = client.post(f"/api/webgate/cron/{job.id}/cancel")
    assert r.status_code == 200, r.text
    message = r.json()["message"]
    assert job.id not in message
    assert "Summarise the week" in message


def test_the_job_label_is_the_name_quoted():
    from types import SimpleNamespace
    from webview.job_routes import job_label
    assert job_label(SimpleNamespace(payload={"name": "Digest"}, task="x")) == "“Digest”"
    assert job_label(None) == t("work.log.a_job")


# --- task 4: one remedy ------------------------------------------------------ #

def test_the_console_scheduler_note_is_the_one_remedy_in_console_words(monkeypatch):
    import cron.service as cs
    import webview.job_routes as jr
    monkeypatch.setattr(cs, "scheduler_off_note",
                        lambda seat="terminal": f"off: `{cs.SCHEDULER_ON_CHAT_LINE}`")
    note = jr.console_scheduler_note()
    assert note == t("work.schedule.off_note")
    assert "`" not in note
    monkeypatch.setattr(cs, "scheduler_off_note", lambda seat="terminal": "")
    assert jr.console_scheduler_note() == ""
    # A probe fault keeps core's own "could not check" sentence, minus backticks.
    monkeypatch.setattr(cs, "scheduler_off_note",
                        lambda seat="terminal": "I could not check `x`.")
    assert jr.console_scheduler_note() == "I could not check x."


def test_every_work_autonomy_off_text_names_one_remedy():
    """Work › Goals, Now & next, Scheduled jobs and the job answer say ONE thing."""
    template = (_REPO / "webview/templates/work.html").read_text(encoding="utf-8")
    keys = set(re.findall(r"data-(?:disabled|sched_disabled)=\"\{\{ t\('([^']+)'\) \}\}\"",
                          template))
    assert keys == {"work.now.off", "work.schedule.off"}
    texts = [STRINGS[k] for k in keys] + [STRINGS["work.schedule.off_note"]]
    for text in texts:
        assert "polyrob autonomy on" not in text and "`" not in text
        assert "open Agent › Settings and let the agent start work on its own, all of it" in text


# --- task 5: a ready goal waits for autonomy; the lane count ----------------- #

def test_a_ready_goal_with_autonomy_off_waits_for_autonomy():
    from core.goal_legibility import status_reason
    goal = {"status": "ready", "payload": {}}
    assert status_reason(goal) == "Waiting its turn."
    assert "turned on" in status_reason(goal, goals_on=False)
    assert "its turn" not in status_reason(goal, goals_on=False)


def test_the_lane_count_sits_beside_its_own_title():
    css = (_REPO / "webview/static/app/app.css").read_text(encoding="utf-8")
    rules = re.findall(r"\.lane > summary\{([^}]*)\}", css)
    assert rules and "justify-content:flex-start" in rules[-1].replace(" ", "")
