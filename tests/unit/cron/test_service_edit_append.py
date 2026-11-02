"""CronService.edit: add a step with no verbatim copy; one delivery validator
(routines §5 / §13).

2026-10-28: three owner-chat loops called ``cronjob_edit(deliver=...)`` ~200
times when the change they promised was TASK text — a flash model could not
copy a 2-5 KB passage as ``old_text``. And ``cronjob_schedule`` stored
``deliver='none'`` verbatim: 9 runs then warned "target 'none' not in allowlist".
"""
import pytest

from cron.jobs import CronJobStore
from cron.schedule import ScheduleError
from cron.service import CronService, normalized_deliver

TASK = "Step 1: read the board.\nStep 2: post the result."


@pytest.fixture
def svc(tmp_path):
    return CronService(CronJobStore(str(tmp_path / "cron.db")))


def _job(svc, **payload):
    return svc.schedule(task=TASK, schedule_spec="1h", user_id="u1", payload=payload or None)


def test_append_text_adds_a_step_at_the_end(svc):
    job = _job(svc)
    assert svc.edit(job.id, user_id="u1", via="agent",
                    append_text="Step 3: repost one winning piece.") == ["task"]
    task = svc.store.get(job.id).task
    assert task.startswith(TASK) and task.endswith("Step 3: repost one winning piece.")


def test_prepend_text_adds_a_rule_at_the_start(svc):
    job = _job(svc)
    svc.edit(job.id, user_id="u1", via="agent", prepend_text="RULE: log only after a send.")
    assert svc.store.get(job.id).task.startswith("RULE: log only after a send.\n\n" + TASK)


def test_replace_and_append_in_one_edit(svc):
    job = _job(svc)
    svc.edit(job.id, user_id="u1", via="agent", old_text="Step 1: read the board.",
             new_text="Step 1: read the board twice.", append_text="Step 3: done.")
    task = svc.store.get(job.id).task
    assert "read the board twice" in task and task.endswith("Step 3: done.")


def test_blank_append_changes_nothing(svc):
    job = _job(svc)
    assert svc.edit(job.id, user_id="u1", via="agent", append_text="   ") == []


def test_whole_task_replace_for_an_owner_seat(svc):
    job = _job(svc, deliver="telegram")
    assert svc.edit(job.id, user_id="u1", via="cli", task="A new task text.") == ["task"]
    stored = svc.store.get(job.id)
    assert stored.task == "A new task text." and stored.payload["deliver"] == "telegram"


def test_whole_task_and_old_text_together_are_refused(svc):
    job = _job(svc)
    with pytest.raises(ScheduleError):
        svc.edit(job.id, user_id="u1", via="cli", task="x", old_text="Step 1", new_text="y")


@pytest.mark.parametrize("word", ["none", "NONE", "", "-", "off"])
def test_schedule_drops_a_no_sink_word_instead_of_storing_it(svc, word):
    job = _job(svc, deliver=word)
    assert "deliver" not in svc.store.get(job.id).payload


def test_schedule_lowercases_a_valid_target(svc):
    job = _job(svc, deliver=" Telegram ")
    assert svc.store.get(job.id).payload["deliver"] == "telegram"


def test_schedule_refuses_an_unknown_target(svc):
    with pytest.raises(ScheduleError, match="unknown delivery target"):
        _job(svc, deliver="pigeon")


def test_edit_none_drops_the_sink_and_a_repeat_is_no_change(svc):
    job = _job(svc, deliver="telegram")
    assert svc.edit(job.id, user_id="u1", via="agent",
                    payload_updates={"deliver": "none"}) == ["deliver"]
    assert "deliver" not in svc.store.get(job.id).payload
    assert svc.edit(job.id, user_id="u1", via="agent",
                    payload_updates={"deliver": "none"}) == []


def test_normalized_deliver_is_the_one_rule():
    assert normalized_deliver(None) is None
    assert normalized_deliver("none") is None
    assert normalized_deliver("TELEGRAM") == "telegram"
    with pytest.raises(ScheduleError):
        normalized_deliver("pigeon")
