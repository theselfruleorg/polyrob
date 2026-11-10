"""107 WS-1: every goal stop reaches the owner once, from the one sweep.

Before: only a run's own block pushed a line, and only with a posture flag on. A crash
reclaim, a dependency cascade and the 14-day age-out sent nothing.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from agents.task.goals import escalation
from agents.task.goals.board import ASK_OPEN, GoalBoard, STATUS_BLOCKED, STATUS_CANCELLED
from core.sqlite_util import execute_retry

LATER = 10 ** 6  # seconds: past the settle time and inside no test window


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


@pytest.fixture(autouse=True)
def bound_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")


@pytest.fixture
def told(monkeypatch):
    sent = []

    async def _push(container, text, attachments=None, priority=None, source=None):
        sent.append((text, source))
        return "sent"

    import core.self_evolution as se
    monkeypatch.setattr(se, "_push_owner_message_outcome", _push)
    return sent


AGENT = SimpleNamespace(container=object())


def _tell(board, *, offset=LATER):
    return asyncio.run(escalation.tell_stops(board, AGENT, now=time.time() + offset))


def _block(board, title="Publish the release notes", error="no write access"):
    g = board.create(user_id="alice", title=title, force=True)
    for _ in range(g.max_retries):
        claimed = board.claim(g.id, "w", ttl_seconds=60)
        assert claimed is not None
        board.record_failure(g.id, error=error, claim_token=board.get(g.id).claim_token)
    assert board.get(g.id).status == STATUS_BLOCKED
    return g


def test_a_blocked_goal_is_told_once_on_the_critical_lane(board, told):
    g = _block(board)
    assert _tell(board) == 1
    assert _tell(board) == 0
    assert len(told) == 1
    assert g.title in told[0][0] and "no write access" in told[0][0]
    assert told[0][1] == "goal_blocked"


def test_the_notice_needs_no_posture_flag(board, told, monkeypatch):
    monkeypatch.setenv("AUTONOMY_POSTURE", "silent")
    _block(board)
    assert _tell(board) == 1


def test_a_stop_waits_for_the_settle_time(board, told):
    _block(board)
    assert _tell(board, offset=0) == 0
    assert told == []


def test_a_crash_reclaim_that_blocks_is_told_with_an_ask(board, told):
    g = board.create(user_id="alice", title="Survey the changelog", max_retries=1)
    board.claim(g.id, "w", ttl_seconds=60)
    execute_retry(board.db_path, "UPDATE goals SET claim_expires=1 WHERE id=?", (g.id,))
    board.reclaim_stale()
    assert board.get(g.id).status == STATUS_BLOCKED
    assert _tell(board) == 1
    assert "worker crashed" in told[0][0]
    assert board.get(g.id).payload["block_kind"] == "needs_input"
    assert [a for a in board.asks(user_id="alice", status=ASK_OPEN)
            if g.id in a.payload["blocks_goal_ids"]]


def test_a_cascade_is_told_and_a_revival_is_not_held_by_its_ask(board, told):
    parent = board.create(user_id="alice", title="Fetch the raw table", max_retries=1)
    child = board.create(user_id="alice", title="Chart that output", depends_on=[parent.id])
    board.claim(parent.id, "w", ttl_seconds=60)
    board.record_failure(parent.id, error="source gone",
                         claim_token=board.get(parent.id).claim_token)
    assert board.get(child.id).payload["block_kind"] == "dep_failed"
    assert _tell(board) == 2
    assert "a goal it depends on stopped" in told[0][0]
    assert child.title in told[0][0] and parent.title in told[0][0]
    assert "/goals" in told[0][0]
    # The prerequisite comes back and completes: the child must reach ready() again.
    board.unblock(parent.id, user_id="alice", rationale="fixed")
    claimed = board.claim(parent.id, "w", ttl_seconds=60)
    assert claimed is not None
    board.record_success(parent.id, claim_token=board.get(parent.id).claim_token)
    assert child.id in [g.id for g in board.ready(limit=5)]


def test_the_age_out_is_told_after_the_block_was_and_closes_the_ask(board, told):
    g = _block(board)
    board.create_ask(user_id="alice", what="Unblock goal", why="w", blocks_goal_ids=[g.id])
    _tell(board)
    told.clear()
    execute_retry(board.db_path, "UPDATE goals SET completed_at=100 WHERE id=?", (g.id,))
    assert board.age_out_blocked(max_age_days=14) == 1
    assert board.get(g.id).status == STATUS_CANCELLED
    assert _tell(board) == 1
    assert g.title in told[0][0] and "cancelled" in told[0][0] and "/goal new" in told[0][0]
    assert _tell(board) == 0
    assert not [a for a in board.asks(user_id="alice", status=ASK_OPEN)
                if g.id in (a.payload or {}).get("blocks_goal_ids", [])]


def test_a_lone_cascade_block_names_its_cause(board, told):
    parent = board.create(user_id="alice", title="Fetch the raw table")
    child = board.create(user_id="alice", title="Chart that output", depends_on=[parent.id])
    assert board.cancel(parent.id, user_id="alice")
    assert board.get(child.id).status == STATUS_BLOCKED
    assert _tell(board) == 1
    assert "a goal it depends on stopped" in told[0][0] and "repeated failures" not in told[0][0]


def test_a_run_block_gets_no_second_ask_from_the_sweep(board, told):
    g = _block(board)
    board.stamp_block_kind(g.id, "needs_input")
    _tell(board)
    assert not [a for a in board.asks(user_id="alice", status=ASK_OPEN)
                if g.id in (a.payload or {}).get("blocks_goal_ids", [])]


def test_a_new_block_after_a_retry_is_a_new_stop(board, told):
    g = _block(board)
    _tell(board)
    board.unblock(g.id, user_id="alice", rationale="try again")
    for _ in range(board.get(g.id).max_retries):
        board.claim(g.id, "w", ttl_seconds=60)
        board.record_failure(g.id, error="still no access",
                             claim_token=board.get(g.id).claim_token)
    # The sweep tells a stop 60 s after it at the earliest, so a new one has a later time.
    execute_retry(board.db_path, "UPDATE goals SET completed_at=completed_at+60 WHERE id=?", (g.id,))
    assert _tell(board) == 1
    assert len(told) == 2 and "still no access" in told[1][0]


def test_an_outage_that_can_heal_waits_until_it_stops_healing(board, told):
    g = _block(board)
    board.stamp_block_kind(g.id, "provider_outage")
    assert _tell(board) == 0
    from agents.task.goals.payload_keys import patch
    patch(board.db_path, g.id, set_keys={"provider_retry_exhausted": True})
    assert _tell(board) == 1


def test_a_requeued_outage_does_not_hide_the_next_real_block(board, told):
    g = _block(board)
    board.stamp_block_kind(g.id, "provider_outage")
    execute_retry(board.db_path, "UPDATE goals SET completed_at=100 WHERE id=?", (g.id,))
    board.age_out_blocked(max_age_days=14, provider_retry_min=30)
    assert "block_kind" not in board.get(g.id).payload
    for _ in range(board.get(g.id).max_retries):
        board.claim(g.id, "w", ttl_seconds=60)
        board.record_failure(g.id, error="stopped at the step limit",
                             claim_token=board.get(g.id).claim_token)
    assert _tell(board) == 1 and "step limit" in told[0][0]


def test_an_outage_tells_when_no_sweep_will_requeue_it(board, told, monkeypatch):
    from core.config_policy import AutonomyConfig
    g = _block(board)
    board.stamp_block_kind(g.id, "provider_outage")
    monkeypatch.setattr(AutonomyConfig, "goal_blocked_max_age_days", staticmethod(lambda: 0))
    assert _tell(board) == 1


def test_a_second_stop_with_the_same_words_is_not_a_repeat_to_the_rail(board, told):
    from core.surfaces.user_delivery import content_hash
    g = _block(board)
    _tell(board)
    board.unblock(g.id, user_id="alice", rationale="again")
    execute_retry(board.db_path, "UPDATE goals SET status='blocked', completed_at=? WHERE id=?",
                  (board.get(g.id).completed_at + 3600 if board.get(g.id).completed_at
                   else time.time() + 3600, g.id))
    _tell(board, offset=LATER + 3600)
    assert len(told) == 2 and content_hash(told[0][0]) != content_hash(told[1][0])


def test_no_known_owner_keeps_the_stop_for_later(board, told, monkeypatch):
    import core.instance as inst
    g = _block(board)
    monkeypatch.setattr(inst, "resolve_owner_user_id", lambda env=None: "")
    assert _tell(board) == 0 and told == []
    assert "stop_told" not in (board.get(g.id).payload or {})


def test_a_run_that_told_the_owner_itself_gets_no_second_line(board, told):
    g = _block(board)
    escalation.mark_stop_told(board, g.id)
    assert _tell(board) == 0


def test_a_message_that_never_reached_the_rail_is_tried_again(board, monkeypatch):
    calls = []

    async def _push(container, text, attachments=None, priority=None, source=None):
        calls.append(text)
        return None if len(calls) == 1 else "fallback"

    import core.self_evolution as se
    monkeypatch.setattr(se, "_push_owner_message_outcome", _push)
    _block(board)
    assert _tell(board) == 0
    assert _tell(board) == 1
    assert _tell(board) == 0  # a rail outcome counts: /missed keeps what it could not deliver
    assert len(calls) == 2


def test_many_stops_are_one_message_with_a_count(board, told):
    for i in range(7):
        _block(board, title=f"Compile section number {i} of {'abcdefg'[i] * 9}")
    assert _tell(board) == 7
    assert len(told) == 1
    assert "…and 2 more." in told[0][0]


def test_an_invented_verb_in_the_reason_does_not_reach_a_line(board, told):
    _block(board, title="First item to compile", error="run /adopt on your side")
    _block(board, title="Second thing altogether", error="plain reason")
    _tell(board)
    assert "/adopt" not in told[0][0]


def test_no_owner_target_touches_nothing(board):
    g = _block(board)
    assert asyncio.run(escalation.tell_stops(board, SimpleNamespace(container=None),
                                             now=time.time() + LATER)) == 0
    assert "stop_told" not in (board.get(g.id).payload or {})


# --- 107 WS-7: cron off holds no goal ------------------------------------------

def _overdue_job(tmp_path):
    from datetime import datetime, timedelta
    from cron.jobs import CronJob, CronJobStore
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="job1", task="SCAN", schedule_spec="1h", user_id="u",
                      next_run_at=datetime.now() - timedelta(hours=3)))


def test_an_overdue_job_holds_goals_only_while_cron_is_on(tmp_path, monkeypatch):
    from datetime import datetime
    from agents.task.goals.dispatcher import imminent_cron_job
    from core.config_policy import AutonomyConfig
    _overdue_job(tmp_path)
    monkeypatch.setattr(AutonomyConfig, "cron_enabled", staticmethod(lambda: True))
    assert imminent_cron_job(str(tmp_path), datetime.now(), 60)
    monkeypatch.setattr(AutonomyConfig, "cron_enabled", staticmethod(lambda: False))
    assert imminent_cron_job(str(tmp_path), datetime.now(), 60) is None


# --- 107 WS-5: an unverified done is told --------------------------------------

def test_an_unverified_line_names_the_reason_and_the_goal(monkeypatch):
    seen = []

    async def _push(container, text, **kw):
        seen.append(text)
        return True

    import core.self_evolution as se
    monkeypatch.setattr(se, "push_owner_message", _push)
    goal = SimpleNamespace(id="abcdefgh1234", title="Ship the notes")
    assert asyncio.run(escalation.tell_unverified(AGENT, goal, "judge error (fail-open): timeout"))
    assert "Ship the notes" in seen[0] and "judge error" in seen[0]
    assert "/goal show abcdefgh" in seen[0]


def test_a_run_that_told_the_owner_without_a_block_marks_nothing(board, told):
    # Before: any run that sent a message stamped the told key, with an old stop time
    # (or the creation time), on a goal that went back to ready.
    g = board.create(user_id="alice", title="Draft the post", force=True)
    escalation.mark_stop_told(board, g.id)
    assert "stop_told" not in (board.get(g.id).payload or {})
