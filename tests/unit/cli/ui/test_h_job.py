"""``/jobs`` and ``/job …`` in the REPL (cli/ui/commands/h_job.py, 080 WS-2).

The REPL runs the SAME handler as Telegram (``surfaces.telegram.job_ops``)
through the ONE owner/admin home seam (``cli._admin_home``). Hermetic:
``POLYROB_DATA_DIR`` points the seam at a temp dir.

C47: "no jobs" and "the scheduler is off" are two different facts; a read
never creates the store.
"""

from __future__ import annotations

import io
from datetime import datetime

import pytest

from cli.ui.commands.h_job import h_job, h_jobs
from cli.ui.commands.registry import CommandContext
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def _on(monkeypatch):
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", lambda: True)


def _ctx(args=(), **kw):
    buf = io.StringIO()
    state = SessionState()
    ctx = CommandContext(renderer=PlainRenderer(state=state, stream=buf), state=state,
                         args=list(args), **kw)
    return ctx, buf


def _run(handler, *args, **kw) -> str:
    ctx, buf = _ctx(args, **kw)
    handler(ctx)
    return buf.getvalue()


def _seed(home, user_id="local"):
    from cron.jobs import CronJob, CronJobStore
    return CronJobStore(str(home / "cron.db")).add(CronJob(
        id="deadbeefcafe1234", task="post daily digest",
        schedule_spec="every monday 09:00", user_id=user_id,
        next_run_at=datetime(2026, 7, 6, 9, 0), created_at=datetime(2026, 7, 1, 12, 0)))


def _jobs(home, user_id="local"):
    from cron.jobs import CronJobStore
    return CronJobStore(str(home / "cron.db")).list(user_id=user_id)


def test_jobs_lists_a_job_with_next_last_and_author(_home, _on):
    _seed(_home)
    out = _run(h_jobs, user_id="local")
    assert "1 scheduled job(s)" in out and "deadbeef" in out
    assert "every monday 09:00" in out and "2026-07-06 09:00" in out
    assert "post daily digest" in out and "last never run" in out
    assert "cron" not in out.lower()


def test_missing_store_is_no_jobs_and_never_created(_home, _on):
    out = _run(h_jobs, user_id="local")
    assert "no scheduled jobs" in out.lower()
    assert "scheduler is off" not in out
    assert not (_home / "cron.db").exists()


def test_scheduler_off_names_the_one_remedy(_home, monkeypatch):
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", lambda: False)
    out = _run(h_jobs, user_id="local")
    assert "scheduler is off" in out and "AUTONOMY_POSTURE full" in out
    assert "CRON_ENABLED" not in out


def test_new_accepts_a_quoted_when(_home, _on):
    # The REPL hands /job its raw words; a quoted <when> is one schedule.
    out = _run(h_job, "new", "'every", "monday", "09:00'", "check", "the", "mail",
               user_id="local")
    assert "Scheduled job" in out
    (job,) = _jobs(_home)
    assert job.schedule_spec == "every monday 09:00" and job.task == "check the mail"


def test_new_bar_grammar_and_tools(_home, _on):
    out = _run(h_job, "new", "1d", "|", "buy", "back", "tools=defi_trade", user_id="local")
    assert "Granted: tools defi_trade" in out
    (job,) = _jobs(_home)
    assert job.payload["tools"] == ["defi_trade"]


def test_pause_run_resume_stop_record_the_repl(_home, _on):
    _seed(_home)
    assert "Paused job" in _run(h_job, "pause", "deadbeef", user_id="local")
    (job,) = _jobs(_home)
    assert job.status == "paused" and job.next_run_at is None
    assert job.payload["paused"]["via"] == "repl"
    assert "resume" in _run(h_job, "run", "deadbeef", user_id="local")
    assert "Resumed job" in _run(h_job, "resume", "deadbeef", user_id="local")
    assert "Stopped job" in _run(h_job, "stop", "deadbeef", user_id="local")
    assert _jobs(_home)[0].payload["stopped"]["via"] == "repl"


def test_tenant_scoped(_home, _on):
    _seed(_home, user_id="someone-else")
    assert "deadbeef" not in _run(h_jobs, user_id="local")
    assert "no match" in _run(h_job, "stop", "deadbeef", user_id="local")
    assert _jobs(_home, "someone-else")[0].status == "scheduled"


def test_unknown_sub_shows_usage(_home, _on):
    assert "Usage: /jobs" in _run(h_job, "frobnicate", user_id="local")


def test_fail_open_on_store_error(_home, _on, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr("surfaces.telegram.job_ops.jobs_reply", boom)
    _seed(_home)
    assert "unavailable" in _run(h_jobs, user_id="local").lower()


def test_cron_is_not_a_repl_verb():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    assert reg.lookup("cron") is None and reg.lookup("crons") is None
    assert reg.lookup("jobs") is not None and reg.lookup("job") is not None
