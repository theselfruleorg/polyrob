"""A rendered job line's fact holds no run time: a time moves with no change to act on."""
from core.status_render import job_fact


def _line(when):
    return f'- abc "digest" · 0 9 * * * · {when} · last: ok 11-07 09:00 · deliver: none'


def test_a_late_job_s_wait_does_not_change_the_fact():
    on_time = job_fact(_line("next 11-08 09:00 UTC"))
    assert job_fact(_line("next 11-08 09:00 UTC, waiting 40m")) == on_time
    assert job_fact(_line("next 11-08 09:00 UTC, waiting 55m")) == on_time
    assert "waiting" not in on_time and "11-08" not in on_time


def test_a_paused_job_stays_a_different_fact():
    assert job_fact(_line("PAUSED")) != job_fact(_line("next 11-08 09:00 UTC"))
