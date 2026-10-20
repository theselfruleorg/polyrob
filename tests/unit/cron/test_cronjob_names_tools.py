"""2026-10-14 review C-9 — the cronjob results name the tools a job's runs carry.

Run 09e3c2be told the owner a job "posts by itself" on a rig with no posting
tool. cronjob_edit / cronjob_schedule / cronjob_show now name the toolset the
runner will start the run with (`tools.cronjob_tools.resolve_cron_tools`, the
runner's own resolver).
"""
import types

import pytest

import tools.goal_tools as gt
from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import (CronEditAction, CronJobTool, CronShowAction,
                                 resolve_cron_tools)


def _tool(tmp_path):
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _ctx():
    return types.SimpleNamespace(user_id="u1", role="orchestrator", is_sub_agent=False,
                                 metadata={}, session_id=None)


def test_runner_reexports_the_one_resolver():
    import cron.runner as r
    assert r.resolve_cron_tools is resolve_cron_tools


@pytest.mark.asyncio
async def test_edit_and_show_name_the_rig_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(gt, "owner_seat_turn", lambda ctx: True)
    t = _tool(tmp_path)
    job = t._cron_service.schedule(task="post the daily update on X", schedule_spec="1d",
                                   user_id="u1")
    res = await t.cronjob_edit(CronEditAction(job_id=job.id, rig="research"),
                               execution_context=_ctx())
    assert res.error is None, res.error
    expected = resolve_cron_tools({"rig": "research"})
    assert "its runs carry: " + ", ".join(expected) in res.extracted_content
    assert "never tell the owner" in res.extracted_content
    show = await t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx())
    assert "its runs carry: " in show.extracted_content
    assert "precheck: none" in show.extracted_content
