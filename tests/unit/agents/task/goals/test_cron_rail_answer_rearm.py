"""2026-10-07 prod: a cron run asked "A) raise the cap, or B) lower the size?",
the owner tapped A, the seat replied "0 goal(s) unblocked", and the answer
waited 6 h for the job's next slot. An approved answer to a cron ask now pulls
the job to the next tick (or right after a run that is still going), and every
seat says where the answer went."""
import asyncio
from datetime import datetime, timedelta

from agents.task.goals.board import GoalBoard
from agents.task.goals.rail_answers import answered_during_run, decision_note
from core.runtime_paths import goals_db_path
from cron.jobs import CronJob, CronJobStore
from cron.scheduler import CronScheduler


def _setup(tmp_path, status="scheduled", hours=6):
    store = CronJobStore(str(tmp_path / "cron.db"))
    store.add(CronJob(id="j1", task="t", schedule_spec="0 */6 * * *", user_id="rob",
                      next_run_at=datetime.now() + timedelta(hours=hours)))
    if status != "scheduled":
        store.set_status("j1", status)
    board = GoalBoard(goals_db_path(str(tmp_path)))
    ask = board.create_ask(user_id="rob", what="A) raise the cap, or B) lower the size?",
                           extra_payload={"rail_id": "cron:j1", "rail_kind": "cron"})
    return store, board, ask


def test_approved_cron_answer_rearms_the_job_now(tmp_path):
    store, board, ask = _setup(tmp_path)
    ok, unblocked = board.decide_ask(ask.id, user_id="rob", approved=True, answer="A")
    assert ok and unblocked == 0
    assert store.get("j1").next_run_at <= datetime.now()
    assert decision_note(board, ask.id, 0) == "Scheduled job j1 runs again now with your answer."


def test_declined_cron_answer_waits_for_the_next_slot(tmp_path):
    store, board, ask = _setup(tmp_path)
    board.decide_ask(ask.id, user_id="rob", approved=False)
    assert store.get("j1").next_run_at > datetime.now() + timedelta(hours=5)
    assert "reads your decline on its next run (" in decision_note(board, ask.id, 0)


def test_running_job_is_not_touched_and_the_note_says_so(tmp_path):
    store, board, ask = _setup(tmp_path, status="running")
    before = store.get("j1").next_run_at
    board.decide_ask(ask.id, user_id="rob", approved=True)
    assert store.get("j1").next_run_at == before
    assert "is running now" in decision_note(board, ask.id, 0)


def test_note_for_goal_counts_and_unrailed_asks(tmp_path):
    board = GoalBoard(goals_db_path(str(tmp_path)))
    assert decision_note(board, "x", 2) == "2 goal(s) unblocked."
    a = board.create_ask(user_id="rob", what="free-standing question here")
    board.decide_ask(a.id, user_id="rob", approved=True)
    assert "No run is waiting on this ask" in decision_note(board, a.id, 0)


def test_answered_during_run_is_bounded_by_the_run_start(tmp_path):
    _, board, ask = _setup(tmp_path)
    started = datetime.now().timestamp()
    board.decide_ask(ask.id, user_id="rob", approved=True)
    assert answered_during_run(board, "rob", "j1", started - 1)
    assert not answered_during_run(board, "rob", "j1", started + 3600)
    assert not answered_during_run(board, "rob", "other", started - 1)


def test_scheduler_reruns_a_job_answered_mid_run(tmp_path):
    """The owner taps while the run is still going: the run ends, and the job is
    due again at once instead of at its next 6 h slot."""
    store = CronJobStore(str(tmp_path / "cron.db"))
    now = datetime.now().replace(microsecond=0) - timedelta(seconds=5)
    store.add(CronJob(id="j1", task="t", schedule_spec="0 */6 * * *", user_id="rob",
                      next_run_at=now - timedelta(minutes=1), max_duration_seconds=0))
    board = GoalBoard(goals_db_path(str(tmp_path)))

    async def run(job):
        ask = board.create_ask(user_id="rob", what="A) raise the cap, or B) skip?",
                               extra_payload={"rail_id": "cron:j1", "rail_kind": "cron"})
        board.decide_ask(ask.id, user_id="rob", approved=True, answer="A")
        return True

    sched = CronScheduler(store, run, lock_path=str(tmp_path / "cron.lock"))
    asyncio.run(sched.tick(now))
    assert store.get("j1").next_run_at == now
