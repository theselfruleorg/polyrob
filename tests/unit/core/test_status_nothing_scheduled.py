"""An agent with autonomy on but no enabled job and no open goal sleeps until a message
comes; the status headline says so. An unknown count is never read as zero."""
import pytest

from core import status_loops
from core.status_render import pause_headline_from


@pytest.fixture(autouse=True)
def _autonomy_on(monkeypatch):
    monkeypatch.setattr(status_loops, "autonomy_idle_reason", lambda started=None: "")


def _live(**kw):
    return {"running_goals": 0, "running_cron": 0, "loops_alive": {"cron": True}, **kw}


def test_nothing_scheduled_is_named():
    line = pause_headline_from({}, _live(scheduled_cron=0, open_goals=0))
    assert "nothing scheduled" in line and "wakes only on a message" in line


@pytest.mark.parametrize("kw", [
    {"scheduled_cron": 1, "open_goals": 0},
    {"scheduled_cron": 0, "open_goals": 2},
    {"open_goals": 0},                     # the cron count is unknown
    {"scheduled_cron": 0},                 # the goal count is unknown
])
def test_a_scheduled_or_unknown_store_reads_as_running(kw):
    line = pause_headline_from({}, _live(**kw))
    assert "nothing scheduled" not in line and line.startswith("▶ RUNNING")


@pytest.mark.parametrize("kw", [
    {"running_cron": 1},                       # a one-shot job runs with enabled=0
    {"turn": {"started": 0}},                  # an owner turn is live
    {"loops_alive": {"cron": False}},          # a dead loop must stay visible
    {"loops_alive": {}},                       # no telemetry: unknown
])
def test_live_work_or_a_dead_loop_keeps_the_full_line(kw):
    line = pause_headline_from({}, _live(scheduled_cron=0, open_goals=0, **kw))
    assert "nothing scheduled" not in line
