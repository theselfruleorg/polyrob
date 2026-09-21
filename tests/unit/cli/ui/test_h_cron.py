"""Tests for the ``/cron`` REPL slash-command handler (cli/ui/commands/h_cron.py).

C17 (2026-09-21): ``/cron`` is no longer read-only — it lists, ADDS and CANCELS
over the same ``cron.service.CronService`` every other seat uses, through the
ONE owner/admin home seam (``cli._admin_home.admin_data_dir``, C2). Hermetic:
``POLYROB_DATA_DIR`` points the seam at a temp dir, so no process-wide cron
state is required and nothing touches the developer's real data home.

C47: "no jobs" and "the ticker is off" are two DIFFERENT facts, and the handler
must never print one as a guess at the other.
"""

from __future__ import annotations

import io
from datetime import datetime

import pytest

from cli.ui.commands.h_cron import h_cron
from cli.ui.commands.registry import CommandContext
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """The admin seam resolves POLYROB_DATA_DIR first — point it at tmp."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def _cron_on(monkeypatch):
    """Report the ticker as ON so the warning line does not mask assertions."""
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", lambda: True)


def _plain_ctx(**overrides):
    """Build a CommandContext with a PlainRenderer writing to a StringIO."""
    buf = io.StringIO()
    state = overrides.pop("state", SessionState())
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, **overrides)
    return ctx, buf


def _seed_job(db_path: str, *, user_id: str = "local", task: str = "post daily digest",
              schedule_spec: str = "every monday 09:00"):
    """Insert one real CronJob into a temp store so the handler reads live data."""
    from cron.jobs import CronJob, CronJobStore

    store = CronJobStore(db_path)
    job = CronJob(
        id="deadbeefcafe1234",
        task=task,
        schedule_spec=schedule_spec,
        user_id=user_id,
        next_run_at=datetime(2026, 7, 6, 9, 0, 0),
        created_at=datetime(2026, 7, 1, 12, 0, 0),
    )
    return store.add(job)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_cron_lists_scheduled_job(_home, _cron_on):
    """A seeded job is rendered as a compact line (short id, status, spec, next run)."""
    _seed_job(str(_home / "cron.db"), user_id="local")

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    out = buf.getvalue()

    assert "Cron jobs (1)" in out
    assert "deadbeef" in out          # short id
    assert "scheduled" in out         # status
    assert "every monday 09:00" in out
    assert "2026-07-06T09:00:00" in out  # next run
    assert "post daily digest" in out    # task preview
    assert "cron" in out.lower()         # title
    assert "/cron cancel" in out         # the verb that acts on what was listed


def test_cron_scopes_to_user(_home, _cron_on):
    """Only the current user's jobs are listed (tenant scoping)."""
    _seed_job(str(_home / "cron.db"), user_id="someone_else")

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    out = buf.getvalue()

    assert "no cron jobs scheduled" in out
    assert "deadbeef" not in out


def test_cron_empty_store_is_graceful(_home, _cron_on):
    """An initialized-but-empty store → friendly one-liner, no jobs."""
    from cron.jobs import CronJobStore

    CronJobStore(str(_home / "cron.db"))  # creates schema, no rows

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    assert "no cron jobs scheduled" in buf.getvalue()


def test_cron_missing_db_never_guesses_why(_home, _cron_on):
    """C47: no cron.db and the ticker ON → "no jobs yet", NOT "not enabled".

    The old line read ``no cron jobs scheduled yet — not enabled``: it inferred
    a FLAG state from a MISSING FILE, so an owner with scheduled runs switched
    on and nothing queued was told his scheduler was off.
    """
    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    out = buf.getvalue()
    assert "no cron jobs scheduled" in out
    assert "not enabled" not in out
    assert "scheduled runs are off" not in out
    assert not (_home / "cron.db").exists()   # a READ never creates the store


def test_cron_ticker_off_says_so_with_the_verb(_home, monkeypatch):
    """C47/E18: the ticker being off is its OWN line, and its remedy is a verb."""
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", lambda: False)
    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    out = buf.getvalue()
    assert "scheduled runs are off" in out
    assert "polyrob autonomy on" in out
    assert "CRON_ENABLED" not in out


def test_cron_ticker_unreadable_says_unknown(_home, monkeypatch):
    """A resolver that REFUSES is unknown, never "off"."""
    def _boom():
        raise RuntimeError("resolver exploded")
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", _boom)
    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    out = buf.getvalue()
    assert "could not read whether scheduled runs are on" in out


def test_cron_default_user_id(_home, _cron_on):
    """A ctx without an explicit user_id defaults to 'local'."""
    _seed_job(str(_home / "cron.db"), user_id="local")

    ctx, buf = _plain_ctx(user_id="")
    h_cron(ctx)
    assert "deadbeef" in buf.getvalue()


def test_cron_fail_open_on_store_error(_home, _cron_on, monkeypatch):
    """A raising store degrades to an '(unavailable: ...)' line, never a crash."""
    (_home / "cron.db").write_text("x")

    def _boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr("cron.service.CronService.list_jobs", _boom)

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)  # must not raise
    assert "unavailable" in buf.getvalue().lower()


def test_cron_disabled_job_annotated(_home, _cron_on):
    """A cancelled/disabled job is annotated (not silently hidden)."""
    from cron.jobs import CronJobStore

    job = _seed_job(str(_home / "cron.db"), user_id="local")
    CronJobStore(str(_home / "cron.db")).cancel(job.id, user_id="local")

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    out = buf.getvalue()
    assert "cancelled" in out
    assert "disabled" in out


# ---------------------------------------------------------------------------
# add / cancel (C17)
# ---------------------------------------------------------------------------


def test_cron_add_schedules_a_real_job(_home, _cron_on):
    """``/cron add 30m <task>`` persists through CronService and is then listed."""
    ctx, buf = _plain_ctx(user_id="local", args=["add", "30m", "check", "the", "mail"])
    h_cron(ctx)
    assert "scheduled" in buf.getvalue()

    from cron.jobs import CronJobStore
    from cron.service import CronService
    jobs = CronService(CronJobStore(str(_home / "cron.db"))).list_jobs(user_id="local")
    assert len(jobs) == 1
    assert jobs[0].task == "check the mail"
    assert jobs[0].schedule_spec == "30m"


def test_cron_add_is_tenant_scoped(_home, _cron_on):
    """A job added here belongs to THIS tenant, and another tenant cannot see it."""
    ctx, _ = _plain_ctx(user_id="u1", args=["add", "30m", "mine"])
    h_cron(ctx)

    ctx2, buf2 = _plain_ctx(user_id="u2")
    h_cron(ctx2)
    assert "no cron jobs scheduled" in buf2.getvalue()


def test_cron_add_bad_schedule_refuses_with_the_grammar(_home, _cron_on):
    """An unparseable spec names WHAT failed and shows the accepted forms."""
    ctx, buf = _plain_ctx(user_id="local", args=["add", "whenever", "do", "it"])
    h_cron(ctx)
    out = buf.getvalue()
    assert "could not read that schedule" in out
    assert "every monday 09:00" in out
    assert not (_home / "cron.db").exists() or \
        _no_jobs(str(_home / "cron.db"))


def _no_jobs(db_path: str) -> bool:
    from cron.jobs import CronJobStore
    return not CronJobStore(db_path).list(user_id="local")


def test_cron_add_without_a_task_shows_usage(_home, _cron_on):
    ctx, buf = _plain_ctx(user_id="local", args=["add", "30m"])
    h_cron(ctx)
    assert "usage: /cron" in buf.getvalue()


def test_cron_cancel_removes_the_job(_home, _cron_on):
    job = _seed_job(str(_home / "cron.db"), user_id="local")

    ctx, buf = _plain_ctx(user_id="local", args=["cancel", job.id])
    h_cron(ctx)
    assert "cancelled" in buf.getvalue()

    from cron.jobs import CronJobStore
    row = CronJobStore(str(_home / "cron.db")).get(job.id)
    assert row is None or row.status == "cancelled"


def test_cron_cancel_accepts_the_short_id(_home, _cron_on):
    """The listing prints an 8-char id, so cancel must take one."""
    job = _seed_job(str(_home / "cron.db"), user_id="local")

    ctx, buf = _plain_ctx(user_id="local", args=["cancel", job.id[:8]])
    h_cron(ctx)
    assert "cancelled" in buf.getvalue()


def test_cron_cancel_unknown_id_is_honest(_home, _cron_on):
    _seed_job(str(_home / "cron.db"), user_id="local")
    ctx, buf = _plain_ctx(user_id="local", args=["cancel", "nosuchjob"])
    h_cron(ctx)
    out = buf.getvalue()
    assert "no scheduled job" in out


def test_cron_cancel_is_tenant_scoped(_home, _cron_on):
    """Another tenant's job id is not cancellable from here."""
    job = _seed_job(str(_home / "cron.db"), user_id="someone_else")
    ctx, buf = _plain_ctx(user_id="local", args=["cancel", job.id])
    h_cron(ctx)
    assert "no scheduled job" in buf.getvalue()

    from cron.jobs import CronJobStore
    row = CronJobStore(str(_home / "cron.db")).get(job.id)
    assert row is not None and row.status == "scheduled"


def test_cron_unknown_verb_shows_usage(_home, _cron_on):
    ctx, buf = _plain_ctx(user_id="local", args=["frobnicate"])
    h_cron(ctx)
    assert "unknown /cron verb" in buf.getvalue()
