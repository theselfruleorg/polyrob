"""2026-10-14 review C-6 — a slower model is not cut by a short per-run cap.

GLM-5.3 cron runs ended at their 180 s cap (~40 s per step), half before
done(). The scheduler now holds a MODEL run to at least 300 s and a
money-class run (`cron.jobs.is_money_job` — priority=money or a money rig) to
at least 900 s, never above the 1800 s ceiling; floors only raise.
"""
from cron.jobs import CronJob
from cron.scheduler import (MODEL_RUN_FLOOR_SECONDS, MONEY_RUN_FLOOR_SECONDS,
                            RUN_CAP_CEILING_SECONDS, effective_cap_seconds)


def _job(cap, payload=None):
    return CronJob(id="j", task="t", schedule_spec="1h", user_id="u", next_run_at=None,
                   max_duration_seconds=cap, payload=payload or {})


def test_model_run_floor_and_own_larger_cap():
    assert effective_cap_seconds(_job(180)) == MODEL_RUN_FLOOR_SECONDS
    assert effective_cap_seconds(_job(1200)) == 1200


def test_money_class_floor_by_priority_and_by_rig():
    assert effective_cap_seconds(_job(180, {"priority": "money"})) == MONEY_RUN_FLOOR_SECONDS
    assert effective_cap_seconds(_job(180, {"rig": "money_rail"})) == MONEY_RUN_FLOOR_SECONDS
    assert effective_cap_seconds(_job(1800, {"rig": "money_rail"})) == RUN_CAP_CEILING_SECONDS


def test_model_free_jobs_and_zero_keep_their_cap():
    assert effective_cap_seconds(_job(60, {"digest": True})) == 60
    assert effective_cap_seconds(_job(60, {"read_verb": {"verb": "x"}})) == 60
    assert effective_cap_seconds(_job(60, {"wake_agent": False})) == 60
    assert effective_cap_seconds(_job(0)) == 0
