"""Tests for the ``/cron`` REPL slash-command handler (cli/ui/commands/h_cron.py).

Hermetic: the real cron DB path is redirected to a temp dir via ``get_data_root``,
and cron state is either a real temp SQLite store or a monkeypatched stub — no
process-wide cron state is required. Mirrors ``tests/unit/cli/ui/test_commands.py``
for CommandContext construction + output capture.
"""

from __future__ import annotations

import io
from datetime import datetime

from cli.ui.commands.h_cron import h_cron
from cli.ui.commands.registry import CommandContext
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


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


def test_cron_lists_scheduled_job(monkeypatch, tmp_path):
    """A seeded job is rendered as a compact line (short id, status, spec, next run)."""
    _seed_job(str(tmp_path / "cron.db"), user_id="local")
    monkeypatch.setattr("core.runtime_config.get_data_root", lambda: str(tmp_path))

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


def test_cron_scopes_to_user(monkeypatch, tmp_path):
    """Only the current user's jobs are listed (tenant scoping)."""
    _seed_job(str(tmp_path / "cron.db"), user_id="someone_else")
    monkeypatch.setattr("core.runtime_config.get_data_root", lambda: str(tmp_path))

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    out = buf.getvalue()

    assert "no cron jobs scheduled" in out
    assert "deadbeef" not in out


def test_cron_empty_store_is_graceful(monkeypatch, tmp_path):
    """An initialized-but-empty store → friendly one-liner, no jobs."""
    from cron.jobs import CronJobStore

    CronJobStore(str(tmp_path / "cron.db"))  # creates schema, no rows
    monkeypatch.setattr("core.runtime_config.get_data_root", lambda: str(tmp_path))

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    assert "no cron jobs scheduled" in buf.getvalue()


def test_cron_missing_db_is_graceful(monkeypatch, tmp_path):
    """No cron.db at all (cron never used / disabled) → friendly one-liner."""
    monkeypatch.setattr("core.runtime_config.get_data_root", lambda: str(tmp_path))

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    out = buf.getvalue()
    assert "no cron jobs scheduled" in out
    assert "not enabled" in out
    assert "cron" in out.lower()


def test_cron_default_user_id(monkeypatch, tmp_path):
    """A ctx without an explicit user_id defaults to 'local'."""
    _seed_job(str(tmp_path / "cron.db"), user_id="local")
    monkeypatch.setattr("core.runtime_config.get_data_root", lambda: str(tmp_path))

    # user_id="" falls back to "local"
    ctx, buf = _plain_ctx(user_id="")
    h_cron(ctx)
    assert "deadbeef" in buf.getvalue()


def test_cron_fail_open_on_store_error(monkeypatch, tmp_path):
    """A raising store degrades to an '(unavailable: ...)' line, never a crash."""
    db = tmp_path / "cron.db"
    db.write_text("x")  # exists → passes the exists() guard, then blows up on read
    monkeypatch.setattr("core.runtime_config.get_data_root", lambda: str(tmp_path))

    def _boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr("cron.service.CronService.list_jobs", _boom)

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)  # must not raise
    assert "unavailable" in buf.getvalue().lower()


def test_cron_disabled_job_annotated(monkeypatch, tmp_path):
    """A cancelled/disabled job is annotated (not silently hidden)."""
    from cron.jobs import CronJobStore

    job = _seed_job(str(tmp_path / "cron.db"), user_id="local")
    CronJobStore(str(tmp_path / "cron.db")).cancel(job.id, user_id="local")
    monkeypatch.setattr("core.runtime_config.get_data_root", lambda: str(tmp_path))

    ctx, buf = _plain_ctx(user_id="local")
    h_cron(ctx)
    out = buf.getvalue()
    assert "cancelled" in out
    assert "disabled" in out
