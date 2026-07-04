"""P5 — CronTicker delegation + agent runner glue (live path stubbed)."""
import asyncio
import types

import pytest

from cron.runner import CronTicker, make_agent_runner, _cron_tick_is_active
from cron.jobs import CronJob
from cron.scheduler import TickResult


@pytest.fixture(autouse=True)
def _clear_autonomy_marker():
    """make_agent_runner marks its session autonomous in a module-global registry;
    without cleanup that leaks into later tests reusing the same session_id (the
    forged-turn/promote gates then deny a genuine owner turn)."""
    yield
    from agents.task.goals import autonomy_marker
    autonomy_marker._SESSIONS.clear()


@pytest.mark.asyncio
async def test_ticker_tick_once_delegates():
    calls = []

    class _Sched:
        async def tick(self, now=None):
            calls.append(now)
            return "ok"

    t = CronTicker(_Sched(), interval_seconds=1)
    assert await t.tick_once(now="N") == "ok"
    assert calls == ["N"]


@pytest.mark.asyncio
async def test_ticker_run_forever_stops_on_event_and_survives_errors():
    ticks = []

    class _Sched:
        async def tick(self, now=None):
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("boom")  # must not kill the loop
            stop.set()

    stop = asyncio.Event()
    t = CronTicker(_Sched(), interval_seconds=0.01)
    await asyncio.wait_for(t.run_forever(stop_event=stop), timeout=2)
    assert len(ticks) >= 2  # kept going after the first tick raised


@pytest.mark.asyncio
async def test_agent_runner_success():
    # W3: the runner now BUILDS the session (create_session) AND runs the loop
    # (run_session). create_session returns {'id': ...} per the live contract.
    captured = {}

    class _TaskAgent:
        async def create_session(self, user_id, request):
            captured["user_id"] = user_id
            captured["request"] = request
            return {"id": "s1"}

        async def run_session(self, user_id, session_id):
            captured["ran"] = (user_id, session_id)
            return "final result"

    runner = make_agent_runner(_TaskAgent())
    job = CronJob(id="j", task="t", schedule_spec="1h", user_id="u1",
                  next_run_at=None, payload={"provider": "anthropic"})
    assert await runner(job) is True
    assert captured["user_id"] == "u1"
    assert captured["ran"] == ("u1", "s1")  # the loop actually ran (bug fix)
    assert "skip_memory" not in captured["request"]  # dead flag removed, not carried
    assert captured["request"]["cron"] is True
    assert captured["request"]["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_agent_runner_failure_returns_false():
    class _TaskAgent:
        async def create_session(self, user_id, request):
            raise RuntimeError("nope")

    runner = make_agent_runner(_TaskAgent())
    job = CronJob(id="j", task="t", schedule_spec="1h", user_id="u1", next_run_at=None)
    assert await runner(job) is False


def test_cron_tick_is_active_true_when_jobs_ran():
    assert _cron_tick_is_active(TickResult(ran=["job-1"])) is True


def test_cron_tick_is_active_true_when_jobs_failed():
    assert _cron_tick_is_active(TickResult(failed=["job-1"])) is True


def test_cron_tick_is_active_false_when_nothing_ran_or_failed():
    assert _cron_tick_is_active(TickResult()) is False


def test_cron_tick_is_active_false_when_only_skipped_locked():
    # skipped_locked is True but ran/failed are still empty -> idle, not active.
    assert _cron_tick_is_active(TickResult(skipped_locked=True)) is False


def test_cron_tick_is_active_false_when_only_skipped_busy():
    # skipped_busy is True but ran/failed are still empty -> idle, not active.
    assert _cron_tick_is_active(TickResult(skipped_busy=True)) is False


def test_cron_ticker_ticks_at_least_once_when_flag_enabled(monkeypatch):
    """With TICKER_IDLE_BACKOFF_ENABLED on, the ticker still fires at least once
    over a short window. The actual idle/active classification logic is proven
    deterministically by the `_cron_tick_is_active` unit tests above — this test
    only exercises the wiring (flag on -> ticker still runs), not the backoff
    timing itself (which IntervalTicker always front-loads one immediate tick
    before any interval wait, making await_count assertions non-diagnostic for
    backoff behavior)."""
    import asyncio
    from unittest.mock import AsyncMock
    from cron.runner import CronTicker
    from cron.scheduler import TickResult

    monkeypatch.setenv("TICKER_IDLE_BACKOFF_ENABLED", "true")
    monkeypatch.setenv("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", "3")

    scheduler = AsyncMock()
    scheduler.tick = AsyncMock(return_value=TickResult())  # empty = idle

    async def run():
        stop = asyncio.Event()
        ticker = CronTicker(scheduler, interval_seconds=1)
        task = asyncio.create_task(ticker.run_forever(stop_event=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert scheduler.tick.await_count >= 1


def test_cron_ticker_backoff_off_by_default_is_fixed_cadence(monkeypatch):
    """Without the flag, behavior is the pre-existing fixed 60s (here shrunk
    to 0.01s) cadence regardless of TickResult contents."""
    import asyncio
    from unittest.mock import AsyncMock
    from cron.runner import CronTicker
    from cron.scheduler import TickResult

    monkeypatch.delenv("TICKER_IDLE_BACKOFF_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)

    scheduler = AsyncMock()
    scheduler.tick = AsyncMock(return_value=TickResult())

    async def run():
        stop = asyncio.Event()
        ticker = CronTicker(scheduler, interval_seconds=0.01)
        task = asyncio.create_task(ticker.run_forever(stop_event=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    # fixed 0.01s cadence over 0.05s -> several ticks, same as before this change
    assert scheduler.tick.await_count >= 2
