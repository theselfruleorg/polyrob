"""The X write gate names WHY a post needs the owner (prod 2026-10-10/11).

The owner-authored promo job d32f28647290 read ``reports/promo-drafts.md`` and
its post waited 170 s for a card; the agent told the owner "the rule is off but
the gate is still on" and could not say what the gate was. Job 540a9bf73129 is
agent-authored, and the agent claimed it "auto-posts". The refusal and the
waiting text now say the real cause, and the owner's remedy, in one line.
"""
import asyncio
import logging
from unittest.mock import MagicMock

import pytest

pytest.importorskip("polyrob_x")

from agents.task.loop_types.execution_context import ActionExecutionContext
from tools.base_tool import ToolStatus
from polyrob_x.twitter_tool import TwitterPostAction, TwitterTool


def _tool(monkeypatch):
    monkeypatch.setenv("TWITTER_ENABLED", "true")
    monkeypatch.setenv("TWITTER_REQUIRE_APPROVAL", "false")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw-cause")
    t.name = "twitter"
    t._status = ToolStatus.HEALTHY
    t._error_message = None
    t._enabled = True
    t._initialized = True
    t._container = MagicMock()
    t._services = {}
    t.client = MagicMock()
    t.api_v1 = MagicMock()
    t._write_times = []
    t._dm_times = []
    return t


def _queue(monkeypatch, *, answer=False, hang=False):
    class _Queue:
        decides_as_owner = True

        async def request(self, name, params, ctx, **kw):
            if hang:
                await asyncio.sleep(5)
            return answer

    import tools.controller.approval as approval
    real = approval.get_approval_provider_or_deny
    monkeypatch.setattr(approval, "get_approval_provider_or_deny",
                        lambda name, **k: _Queue() if name == "owner_queue" else real(name, **k))
    monkeypatch.setattr(approval, "approval_wait_timeout_sec", lambda *a, **k: 0.05)


def _cron(sid, job_id, *, owner_job):
    from core import autonomy_marker as am
    am.mark_autonomous(sid, cron_job_id=job_id)
    if owner_job:
        am.note_owner_job(job_id, "PROMO CONTENT — draft, approve, post")
    ctx = ActionExecutionContext(session_id=sid, user_id="u1", role="orchestrator")
    ctx.metadata = {}
    return ctx


@pytest.mark.asyncio
async def test_a_tainted_owner_job_names_the_source_it_read(monkeypatch):
    from core.security import read_taint
    t = _tool(monkeypatch)
    _queue(monkeypatch, hang=True)
    ctx = _cron("cron-promo-1", "d32f28647290", owner_job=True)
    read_taint.note_result(None, ctx, "filesystem_read_file", "filesystem",
                           MagicMock(extracted_content="draft", error=None),
                           params={"file_path": "reports/promo-drafts.md"})
    res = await t.twitter_post(TwitterPostAction(text="I run on POLYROB"),
                               execution_context=ctx)
    assert res.error and "waiting for the owner's approval" in res.error
    assert ("this run read an untrusted source: filesystem_read_file "
            "reports/promo-drafts.md") in res.error
    assert "read_taint" in res.error            # the owner's setting is named
    t.client.create_tweet.assert_not_called()


@pytest.mark.asyncio
async def test_an_agent_authored_job_says_so_and_names_adopt(monkeypatch):
    t = _tool(monkeypatch)
    _queue(monkeypatch, answer=False)
    ctx = _cron("cron-release-1", "540a9bf73129", owner_job=False)
    res = await t.twitter_post(TwitterPostAction(text="v1.3.0 is out"), execution_context=ctx)
    assert res.error and "approval denied" in res.error
    assert "this job is agent-authored: 540a9bf73129" in res.error
    assert "/adopt 540a9bf73129" in res.error
    t.client.create_tweet.assert_not_called()


@pytest.mark.asyncio
async def test_an_untainted_owner_job_still_posts_without_a_card(monkeypatch):
    t = _tool(monkeypatch)
    _queue(monkeypatch, answer=False)
    t.client.create_tweet.return_value = MagicMock(data={"id": "9", "text": "x"})
    ctx = _cron("cron-promo-2", "d32f28647290-b", owner_job=True)
    res = await t.twitter_post(TwitterPostAction(text="hello"), execution_context=ctx)
    assert res.error is None, res.error
    t.client.create_tweet.assert_called_once()


def test_the_x_pack_registers_its_write_receipts_as_untainting():
    from core.security import read_taint
    from polyrob_x.registration import register_twitter_tool
    register_twitter_tool(force=True)
    ok = MagicMock(extracted_content="🐦 Posted tweet 1", error=None)
    for verb in ("twitter_post", "twitter_reply", "twitter_quote", "twitter_thread",
                 "twitter_like", "twitter_follow", "twitter_dm", "twitter_whoami"):
        assert not read_taint.result_is_untrusted(verb, "twitter", ok), verb
    for verb in ("twitter_get_dms", "twitter_get_timeline", "twitter_get_mentions",
                 "twitter_poll_results", "twitter_search"):
        assert read_taint.result_is_untrusted(verb, "twitter", ok), verb
