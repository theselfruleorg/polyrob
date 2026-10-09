"""Who cancelled a cron job — answerable from the job itself (2026-10-06).

Prod: the owner asked Rob 'who canceled the promo content job?'. Maintenance had
cancelled 228b02251501 from the CLI at 10-05 20:29Z. The service did record a
`cron_cancelled` event, but with `via=""`, and `cronjob_show` never read it, so
Rob could only guess.
"""
import types

import pytest

from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import CronCancelAction, CronJobTool, CronShowAction


@pytest.fixture
def evlog(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    p = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(p))
    import core.event_log as el
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    return p


def _tool(tmp_path):
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _ctx():
    return types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                                 metadata={}, session_id=None)


@pytest.mark.asyncio
async def test_show_names_when_and_via_for_a_cancelled_job(tmp_path, evlog):
    t = _tool(tmp_path)
    job = t._cron_service.schedule(task="post promo", schedule_spec="1h", user_id="u1")
    assert t._cron_service.cancel(job.id, user_id="u1", via="cli")
    out = (await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx())).extracted_content
    assert "cancelled:" in out and "via cli" in out


@pytest.mark.asyncio
async def test_agent_cancel_is_recorded_as_agent(tmp_path, evlog):
    t = _tool(tmp_path)
    job = t._cron_service.schedule(task="x", schedule_spec="1h", user_id="u1")
    await t.cronjob_cancel(CronCancelAction(job_id=job.id), execution_context=_ctx())
    out = (await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx())).extracted_content
    assert "via agent" in out


@pytest.mark.asyncio
async def test_live_job_has_no_cancel_line(tmp_path, evlog):
    t = _tool(tmp_path)
    job = t._cron_service.schedule(task="x", schedule_spec="1h", user_id="u1")
    out = (await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx())).extracted_content
    assert "cancelled:" not in out


def test_cli_cancel_passes_via_cli():
    import inspect
    from cli.commands import cron as cron_cli
    src = inspect.getsource(cron_cli.cancel.callback)
    assert 'via="cli"' in src


def test_every_owner_seat_cancel_passes_via():
    """Telegram `/cron cancel` and the console cancel record their surface too,
    so `cronjob_show` never says 'unrecorded surface' for a post-10-06 cancel."""
    import inspect
    from surfaces.telegram import owner_ops
    from webview import pages
    assert 'svc.cancel(job_id, user_id=user_id, via="telegram")' in inspect.getsource(owner_ops)
    assert 'service.cancel(job_id, user_id=user_id, via="webview")' in inspect.getsource(pages.api_cron_cancel)
