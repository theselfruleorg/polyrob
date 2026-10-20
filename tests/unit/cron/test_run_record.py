"""R2 + R3 of the 2026-10-13 memory/coherency review, cron half.

- B5/C-03: every exit path writes the job's run record; a run that did work and
  then stopped before done() (step budget, time cap, pause) is PARTIAL with a
  summary of what it sent, did and signed — never "failed" with no summary.
- B6: the next run of the same job reads its own last runs as <previous-runs>.
- B4: owner decisions are consumed only after a run that completed.
- C-11: a run that declared BLOCKED / SKIPPED is not recorded as done.
- B8: a public sink never receives the run's closing "Done — cost" line.
"""
import asyncio

import pytest

from agents.task.runtime.run_outcome import RunOutcome
from cron import runner as cron_runner
from cron.jobs import CronJob
from cron.runner import make_agent_runner

TX = "0x" + "ab" * 32


def _job(**kw):
    base = dict(id="j1", task="buy back PNL", schedule_spec="30m", user_id="u1",
                next_run_at=None, one_shot=True, skip_memory=True,
                max_duration_seconds=180, payload=kw.pop("payload", {}), created_at=None)
    base.update(kw)
    return CronJob(**base)


class _Agent:
    container = None


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    records, events, tasks = [], [], []

    async def _fin(**kw):
        records.append(kw)

    async def _no_prev(**kw):
        return []

    monkeypatch.setattr("modules.memory.episodic.finalize_episode", _fin)
    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", _no_prev)
    monkeypatch.setattr(cron_runner, "_cron_ev",
                        lambda job, outcome, reason=None, **kw: events.append((outcome, reason)))
    return {"records": records, "events": events, "tasks": tasks, "dir": str(tmp_path)}


def _patch_run(monkeypatch, env, outcome: RunOutcome):
    async def fake_run(task_agent, *, user_id, request, **kw):
        env["tasks"].append(request["task"])
        return outcome
    monkeypatch.setattr(cron_runner, "_run_task_to_outcome", fake_run)


@pytest.mark.asyncio
async def test_a_run_that_bought_then_ran_out_of_steps_is_partial(monkeypatch, env):
    _patch_run(monkeypatch, env, RunOutcome(
        session_id="s1", status="Session completed successfully", done_called=False,
        user_messages=[f"Bought 0.1 ETH of PNL, tx {TX}"],
        artifacts=[{"kind": "defi_swap", "detail": f"swap confirmed {TX}"}], steps=20))
    ok = await make_agent_runner(_Agent(), data_dir=env["dir"])(_job())
    assert ok is False                       # still not a completed money run
    rec = env["records"][-1]
    assert rec["outcome"] == "partial"
    assert rec["thread_key"] == "cron:j1" and rec["run_record"] is True
    assert TX in rec["summary"] and "stopped before done()" in rec["summary"]
    assert rec["meta"]["tx"] == [TX]
    assert ("partial", "incomplete_no_done") in env["events"]


@pytest.mark.asyncio
async def test_a_run_that_did_nothing_and_ran_out_is_failed_with_a_summary(monkeypatch, env):
    _patch_run(monkeypatch, env, RunOutcome(
        session_id="s1", status="Session completed successfully", done_called=False))
    await make_agent_runner(_Agent(), data_dir=env["dir"])(_job())
    rec = env["records"][-1]
    assert rec["outcome"] == "failed" and rec["summary"]


@pytest.mark.asyncio
async def test_a_stopped_run_names_its_typed_stop_reason(monkeypatch, env):
    """083 WS-1: the record states WHY the run stopped, with n/max steps."""
    _patch_run(monkeypatch, env, RunOutcome(
        session_id="s1", status="Session completed successfully", done_called=False,
        stop_reason="max_steps", steps=20, max_steps=20))
    await make_agent_runner(_Agent(), data_dir=env["dir"])(_job())
    rec = env["records"][-1]
    assert "stopped before done() — stopped at the step limit (20/20)" in rec["summary"]
    assert rec["meta"]["stop_reason"] == "max_steps"


@pytest.mark.asyncio
async def test_a_run_stopped_at_the_cap_records_its_last_report(monkeypatch, env):
    """083 WS-2: the agent's last report is the record's summary, labelled as
    its words; the run is still not done."""
    _patch_run(monkeypatch, env, RunOutcome(
        session_id="s1", status="Session completed successfully", done_called=False,
        stop_reason="max_steps", steps=20, max_steps=20, exhausted_summary="Half done."))
    ok = await make_agent_runner(_Agent(), data_dir=env["dir"])(_job())
    assert ok is False
    rec = env["records"][-1]
    assert rec["outcome"] == "failed"
    assert rec["summary"] == ("FAILED: stopped before done() — stopped at the step limit "
                              "(20/20) | Last report (agent's words): Half done.")


@pytest.mark.asyncio
async def test_a_run_cut_by_its_cap_still_writes_its_record(monkeypatch, env):
    """B5: the scheduler's wait_for CANCELS the runner — no RunOutcome returns."""
    from core.autonomy_marker import mark_autonomous
    started = asyncio.Event()

    async def hung_run(task_agent, *, user_id, request, cron_job_id=None, **kw):
        mark_autonomous(request["session_id"], None, cron_job_id=cron_job_id)
        started.set()
        await asyncio.sleep(30)

    async def resident(task_agent, session_id, status):
        return RunOutcome(session_id=session_id, done_called=False,
                          user_messages=[f"swap sent {TX}"])

    monkeypatch.setattr(cron_runner, "_run_task_to_outcome", hung_run)
    monkeypatch.setattr("agents.task.runtime.run_outcome.build_run_outcome", resident)
    task = asyncio.create_task(make_agent_runner(_Agent(), data_dir=env["dir"])(_job()))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rec = env["records"][-1]
    assert rec["outcome"] == "partial" and TX in rec["summary"]
    assert "cut by an owner pause before done()" in rec["summary"]


@pytest.mark.asyncio
async def test_a_run_cut_at_its_time_cap_names_the_wall_clock_stop(monkeypatch, env):
    """083: a cancel at (or past) the job's effective cap is the time cap — the
    record carries the wall_clock stop reason, never the owner-pause text."""
    from core.autonomy_marker import mark_autonomous
    started = asyncio.Event()

    async def hung_run(task_agent, *, user_id, request, cron_job_id=None, **kw):
        mark_autonomous(request["session_id"], None, cron_job_id=cron_job_id)
        started.set()
        await asyncio.sleep(30)

    async def resident(task_agent, session_id, status):
        return RunOutcome(session_id=session_id, done_called=False,
                          user_messages=[f"swap sent {TX}"])

    monkeypatch.setattr(cron_runner, "_run_task_to_outcome", hung_run)
    monkeypatch.setattr("agents.task.runtime.run_outcome.build_run_outcome", resident)
    monkeypatch.setattr("cron.scheduler.effective_cap_seconds", lambda job: 0.5)
    task = asyncio.create_task(make_agent_runner(_Agent(), data_dir=env["dir"])(_job()))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rec = env["records"][-1]
    assert rec["outcome"] == "partial"
    assert "cut by the wall-clock cap (0.5s) before done()" in rec["summary"]
    assert rec["meta"]["stop_reason"] == "wall_clock"


@pytest.mark.asyncio
async def test_a_run_whose_call_raised_still_writes_its_record(monkeypatch, env):
    """The run call raised after its session started: `session_id` was never
    assigned, so the record used to be skipped on this exit path."""
    from core.autonomy_marker import mark_autonomous

    async def raising_run(task_agent, *, user_id, request, cron_job_id=None, **kw):
        mark_autonomous(request["session_id"], None, cron_job_id=cron_job_id)
        raise RuntimeError("provider exploded mid-run")

    async def resident(task_agent, session_id, status):
        return RunOutcome(session_id=session_id, done_called=False,
                          user_messages=[f"swap sent {TX}"])

    monkeypatch.setattr(cron_runner, "_run_task_to_outcome", raising_run)
    monkeypatch.setattr("agents.task.runtime.run_outcome.build_run_outcome", resident)
    ok = await make_agent_runner(_Agent(), data_dir=env["dir"])(_job())
    assert ok is False
    rec = env["records"][-1]
    assert rec["outcome"] == "partial" and rec["thread_key"] == "cron:j1"
    assert "provider exploded mid-run" in rec["summary"] and TX in rec["summary"]


@pytest.mark.asyncio
async def test_a_raise_before_the_session_existed_writes_no_record(monkeypatch, env):
    async def raising_run(task_agent, *, user_id, request, **kw):
        raise RuntimeError("create_session failed")   # never marked

    monkeypatch.setattr(cron_runner, "_run_task_to_outcome", raising_run)
    await make_agent_runner(_Agent(), data_dir=env["dir"])(_job(id="j-nosess"))
    assert env["records"] == []


@pytest.mark.asyncio
async def test_the_next_run_reads_its_own_previous_runs(monkeypatch, env):
    from modules.memory.provider import EpisodeRecord
    prev = EpisodeRecord(ts=1_760_000_000, user_id="u1", session_id="old", kind="cron",
                         task="buy back PNL", outcome="partial", summary="PARTIAL: stopped",
                         thread_key="cron:j1",
                         meta={"job_id": "j1", "reason": "stopped before done()",
                               "sent": ["told the owner: bought"], "tx": [TX]})
    seen = {}

    async def recall(**kw):
        seen.setdefault("calls", []).append(kw)
        return [prev] if kw.get("thread_key") == "cron:j1" else []

    monkeypatch.setattr("modules.memory.registry.memory_recall_episodes", recall)
    _patch_run(monkeypatch, env, RunOutcome(session_id="s2", done_called=True,
                                            done_text="ok", status="x"))
    await make_agent_runner(_Agent(), data_dir=env["dir"])(_job())
    task = env["tasks"][-1]
    assert '<previous-runs rail="cron:j1">' in task
    assert TX in task and "PARTIAL" in task and "told the owner: bought" in task
    assert task.rstrip().endswith("buy back PNL"), "the job text stays last"


@pytest.mark.asyncio
async def test_a_decline_survives_a_failed_run_and_is_consumed_after_a_completed_one(
        monkeypatch, env):
    """B4: answers used to be marked consumed BEFORE the run."""
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import goals_db_path
    board = GoalBoard(goals_db_path(env["dir"]))
    ask = board.create_ask(user_id="u1", what="Post the buyback on X?",
                           extra_payload={"rail_id": "cron:j1", "rail_kind": "cron"})
    board.decide_ask(ask.id, user_id="u1", approved=False)
    run = make_agent_runner(_Agent(), data_dir=env["dir"])

    _patch_run(monkeypatch, env, RunOutcome(session_id="s1", done_called=False, status="x"))
    await run(_job())
    assert "DECLINED" in env["tasks"][-1]
    assert not board.get(ask.id).payload.get("consumed_by_run")

    _patch_run(monkeypatch, env, RunOutcome(session_id="s2", done_called=True,
                                            done_text="ok", status="x"))
    await run(_job())
    assert "DECLINED" in env["tasks"][-1], "the failed run did not spend the decline"
    assert board.get(ask.id).payload.get("consumed_by_run")


@pytest.mark.asyncio
async def test_a_declared_blocked_run_is_not_done(monkeypatch, env):
    _patch_run(monkeypatch, env, RunOutcome(
        session_id="s1", status="x", done_called=True,
        done_text="OUTCOME: BLOCKED — owner grant for defi_trade",
        outcome_line="BLOCKED — owner grant for defi_trade", blocked=True,
        blocked_need="owner grant for defi_trade"))
    await make_agent_runner(_Agent(), data_dir=env["dir"])(_job())
    assert env["records"][-1]["outcome"] == "blocked"
    assert env["events"][-1][0] == "blocked"


@pytest.mark.asyncio
async def test_a_declared_skip_is_not_done(monkeypatch, env):
    _patch_run(monkeypatch, env, RunOutcome(
        session_id="s1", status="x", done_called=True,
        done_text="SKIPPED — slot cap reached, no buy this cycle"))
    await make_agent_runner(_Agent(), data_dir=env["dir"])(_job())
    assert env["records"][-1]["outcome"] == "skipped"
    assert env["events"][-1][0] == "skipped"


def test_a_public_sink_never_gets_the_closing_line():
    from cron.delivery import report_body
    run = RunOutcome(session_id="s", user_messages=["PNL buyback: 0.1 ETH in.",
                                                    "Done — cost $0.40"])
    assert report_body(run, "twitter") == "PNL buyback: 0.1 ETH in."
    assert report_body(run, "telegram") == "Done — cost $0.40"
    only_closing = RunOutcome(session_id="s", user_messages=["✅ Done — cost $0.40"])
    assert report_body(only_closing, "twitter") == ""


@pytest.mark.asyncio
async def test_run_record_is_written_with_the_episodic_posture_off(monkeypatch):
    """The record is the job's own continuity: the regime seam decides it."""
    from core.config_policy import AutonomyConfig
    from modules.memory import episodic
    monkeypatch.setattr(AutonomyConfig, "episodic_memory_enabled", staticmethod(lambda: False))
    seen = []

    async def rec(episode, **kw):
        seen.append(episode)

    monkeypatch.setattr("modules.memory.registry.memory_record_episode", rec)
    await episodic.finalize_episode(session_id="s", user_id="u1", kind="cron",
                                    outcome="partial", thread_key="cron:j1", run_record=True)
    await episodic.finalize_episode(session_id="s2", user_id="u1", kind="cron",
                                    outcome="done")
    assert [e.session_id for e in seen] == ["s"]
    assert seen[0].thread_key == "cron:j1"
