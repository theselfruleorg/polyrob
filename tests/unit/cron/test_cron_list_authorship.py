"""The cron job listing names who authored each job (prod 2026-10-11).

The agent told the owner that job 540a9bf73129 "auto-posts, no approval card":
its task text says so, but its payload is ``authored_by: agent``, and an
agent-authored job never posts without a per-post owner approval.
``cronjob_list`` showed no author, and ``cronjob_show`` showed the word but not
what it means.
"""
import asyncio
import types

from cron.jobs import CronJobStore
from cron.service import CronService
from tools.cronjob_tools import CronJobTool, CronListAction, CronShowAction


def _tool(tmp_path):
    t = object.__new__(CronJobTool)
    t._cron_service = CronService(CronJobStore(str(tmp_path / "cron.db")))
    return t


def _ctx():
    return types.SimpleNamespace(user_id="rob", role="orchestrator", metadata={},
                                 session_id="s", is_sub_agent=False)


def test_the_list_names_the_author_of_each_job(tmp_path):
    t = _tool(tmp_path)
    svc = t._cron_service
    svc.schedule(task="PROMO CONTENT", schedule_spec="1h", user_id="rob",
                 payload={"authored_by": "owner"})
    svc.schedule(task="RELEASE CHECK", schedule_spec="2h", user_id="rob",
                 payload={"authored_by": "agent", "legacy_unstamped": True})
    out = asyncio.run(t.cronjob_list(CronListAction(), execution_context=_ctx()))
    lines = out.extracted_content.splitlines()
    promo = next(line for line in lines if "PROMO CONTENT" in line)
    release = next(line for line in lines if "RELEASE CHECK" in line)
    assert "owner-authored" in promo
    assert "agent-authored" in release


def test_show_says_what_agent_authorship_means(tmp_path):
    t = _tool(tmp_path)
    job = t._cron_service.schedule(task="RELEASE CHECK", schedule_spec="2h", user_id="rob",
                                   payload={"authored_by": "agent"})
    out = asyncio.run(t.cronjob_show(CronShowAction(job_id=job.id), execution_context=_ctx()))
    text = out.extracted_content
    assert "authored by: agent" in text
    assert "owner approval" in text and f"/adopt {job.id}" in text
