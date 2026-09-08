"""FIX 3 — a cron run cut off while an OWNER APPROVAL is pending is not a failure.

`cron/service.py::_DEFAULT_MAX_DURATION_S` is 180s and `cron/scheduler.py`
enforces it with `asyncio.wait_for`, recording every `TimeoutError` as a real
failure. But the durable `owner_queue` approval provider gets a 300s wait
(`payment_approval_timeout_sec()`), so an approval-gated verb attempted inside a
cron run is cut off 120s BEFORE the owner's window even elapses — and the job is
blamed for a timeout whose real cause is an unanswered owner prompt. The durable
ask survives, so the work is still redeemable by `/approve`; only the accounting
was wrong.

Boundary implemented here: the timeout is recorded as DEFERRED (not a failure)
IFF this run itself opened a NEW open `tool_approval` ask for the job's tenant —
i.e. an ask created at/after the run started. That is self-bounding: on the next
attempt the same ask is pre-existing (the provider dedups by request hash), so a
second cut-off counts as a genuine failure and nothing loops forever.

A genuine timeout with NO open ask still fails exactly as before, and so does a
pause-cancel racing a timeout (ae18649b).
"""
import asyncio
import time
from datetime import datetime

import pytest

from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler

NOW = datetime(2026, 6, 6, 12, 0, 0)


def _store(tmp_path):
    return CronJobStore(str(tmp_path / "cron.db"))


def _job(**kw):
    base = dict(id="j1", task="t", schedule_spec="*/15 * * * *", user_id="u1",
                next_run_at=datetime(2026, 6, 6, 11, 0, 0), max_duration_seconds=0)
    base.update(kw)
    return CronJob(**base)


def _sched(store, tmp_path, runner):
    return CronScheduler(store, runner, lock_path=str(tmp_path / "tick.lock"))


async def _never_returns(job):
    await asyncio.sleep(5)
    return True


def _board(tmp_path):
    from agents.task.goals.board import GoalBoard
    return GoalBoard(str(tmp_path / "goals.db"))


def _open_tool_approval_ask(tmp_path, user_id="u1"):
    from tools.controller.approval_queue import TOOL_APPROVAL_ASK_KIND
    return _board(tmp_path).create_ask(
        user_id=user_id, what="Approve x402_invoice_x402_request? [abc]",
        why="tool=x402_invoice_x402_request", force=True,
        extra_payload={"ask_kind": TOOL_APPROVAL_ASK_KIND,
                       "tool_name": "x402_invoice_x402_request",
                       "request_hash": "abc"})


@pytest.mark.asyncio
async def test_timeout_with_no_open_ask_is_still_a_failure(tmp_path):
    """The invariant that must NOT weaken."""
    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00"))
    result = await _sched(s, tmp_path, _never_returns).tick(now=NOW)
    assert s.get("o").status == "failed"
    assert result.failed == ["o"] and not getattr(result, "deferred", [])


@pytest.mark.asyncio
async def test_timeout_while_an_owner_ask_is_open_is_deferred_not_failed(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00"))

    async def runner(job):
        _open_tool_approval_ask(tmp_path)   # the run raised an owner ask...
        await asyncio.sleep(5)              # ...and is then cut off waiting on it
        return True

    result = await _sched(s, tmp_path, runner).tick(now=NOW)
    assert result.deferred == ["o"], "an unanswered owner prompt is not the job's failure"
    assert result.failed == []
    got = s.get("o")
    assert got.status == "scheduled", \
        "a one-shot must stay redeemable — /approve leaves a one-shot grant for the retry"
    assert got.next_run_at is not None


@pytest.mark.asyncio
async def test_a_recurring_job_deferred_reschedules_normally(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="r", schedule_spec="*/15 * * * *"))

    async def runner(job):
        _open_tool_approval_ask(tmp_path)
        await asyncio.sleep(5)
        return True

    result = await _sched(s, tmp_path, runner).tick(now=NOW)
    assert result.deferred == ["r"] and result.failed == []
    assert s.get("r").status == "scheduled"
    assert s.get("r").next_run_at == datetime(2026, 6, 6, 12, 15, 0)


@pytest.mark.asyncio
async def test_a_pre_existing_ask_does_not_excuse_the_second_timeout(tmp_path):
    """Self-bounding: only an ask THIS run opened defers it."""
    _open_tool_approval_ask(tmp_path)
    time.sleep(0.01)
    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00"))
    result = await _sched(s, tmp_path, _never_returns).tick(now=NOW)
    assert result.failed == ["o"] and result.deferred == []
    assert s.get("o").status == "failed"


@pytest.mark.asyncio
async def test_another_tenants_ask_never_excuses_this_job(tmp_path):
    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00"))

    async def runner(job):
        _open_tool_approval_ask(tmp_path, user_id="someone-else")
        await asyncio.sleep(5)
        return True

    result = await _sched(s, tmp_path, runner).tick(now=NOW)
    assert result.failed == ["o"] and result.deferred == []


@pytest.mark.asyncio
async def test_a_decided_ask_no_longer_defers(tmp_path):
    """Only an OPEN ask defers — an answered one means the run had its decision."""
    from agents.task.goals.board import ASK_OPEN

    s = _store(tmp_path)
    s.add(_job(id="o", one_shot=True, schedule_spec="2026-06-06T11:00:00"))

    async def runner(job):
        ask = _open_tool_approval_ask(tmp_path)
        _board(tmp_path).decide_ask(ask.id, user_id="u1", approved=True)
        await asyncio.sleep(5)
        return True

    result = await _sched(s, tmp_path, runner).tick(now=NOW)
    assert _board(tmp_path).asks(user_id="u1", status=ASK_OPEN) == []
    assert result.failed == ["o"] and result.deferred == []
