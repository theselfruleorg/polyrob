"""A store error is logged with its trace, never swallowed (proposal 125 S16, B-H11).

And the goal dispatcher fails closed when its provider check raises (owner
decision D9): it dispatches nothing that tick and warns once.
"""
import logging

import pytest

from modules.goals.board import GoalBoard
from cron.jobs import CronJobStore


def test_a_failed_goal_event_write_is_logged(tmp_path, monkeypatch, caplog):
    import modules.goals.board as board_mod
    board = GoalBoard(str(tmp_path / "goals.db"))

    def _boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(board_mod, "execute_retry", _boom)
    with caplog.at_level(logging.WARNING, logger=board_mod.__name__):
        board._event("g1", "edited", {})
    rec = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert rec and rec[0].exc_info


def test_an_unreadable_cron_store_reclaims_nothing_and_logs(tmp_path, monkeypatch, caplog):
    import core.cron_store as jobs_mod
    store = CronJobStore(str(tmp_path / "cron.db"))

    def _boom(*a, **k):
        raise RuntimeError("database disk image is malformed")
    monkeypatch.setattr(jobs_mod, "execute_retry", _boom)
    with caplog.at_level(logging.WARNING, logger=jobs_mod.__name__):
        assert store.reclaim_stale_running() == 0
    rec = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert rec and rec[0].exc_info


@pytest.mark.asyncio
async def test_episode_evidence_failure_is_logged(caplog):
    from modules.memory import episodic

    class _Tracker:
        async def get_session_breakdown(self, sid):
            raise RuntimeError("usage db locked")

    class _Orch:
        agents = {}
        usage_tracker = _Tracker()
        session_id = "s1"

    with caplog.at_level(logging.WARNING, logger=episodic.__name__):
        await episodic.collect_provenance(_Orch())
    rec = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert rec and rec[0].exc_info


@pytest.mark.asyncio
async def test_dispatch_fails_closed_when_the_provider_check_raises(tmp_path, monkeypatch, caplog):
    from agents.task.goals.dispatcher import GoalDispatcher
    for k in ("AUTONOMY_HALT", "TREASURY_ENTRY_PAUSE", "STREAM_SEEDING_PAUSE", "DATA_ROOT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("GOALS_ENABLED", "true")
    board = GoalBoard(str(tmp_path / "goals.db"))
    board.create(user_id="alice", title="work")
    d = GoalDispatcher(board, task_agent=object())

    def _raise():
        raise RuntimeError("runtime config unreadable")
    monkeypatch.setattr(d, "dispatch_blocked_by_providers", _raise)
    with caplog.at_level(logging.WARNING):
        assert await d.dispatch_once() == 0
        assert await d.dispatch_once() == 0
    warned = [r for r in caplog.records if "provider check failed" in r.getMessage()]
    assert len(warned) == 1
    assert board.list(user_id="alice")[0].status == "ready"
