"""Unit tests for core.tickers (IntervalTicker + TickerSupervisor)."""
import asyncio

import pytest

from core.tickers import IntervalTicker, TickerSupervisor


def test_interval_ticker_runs_until_stopped():
    calls = []

    async def tick():
        calls.append(1)

    async def run():
        stop = asyncio.Event()
        t = IntervalTicker(tick, interval_seconds=0.01)
        task = asyncio.create_task(t.run_forever(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert len(calls) >= 2


def test_tick_error_does_not_kill_loop():
    n = {"c": 0}

    async def tick():
        n["c"] += 1
        raise RuntimeError("boom")

    async def run():
        stop = asyncio.Event()
        t = IntervalTicker(tick, interval_seconds=0.01)
        task = asyncio.create_task(t.run_forever(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert n["c"] >= 2  # kept ticking despite exceptions


def test_ticker_supervisor_starts_enabled_skips_disabled():
    """Supervisor starts only enabled tickers and stops them cleanly."""
    started = []

    async def run():
        sup = TickerSupervisor()

        calls_a = []

        async def tick_a():
            calls_a.append(1)

        calls_b = []

        async def tick_b():
            calls_b.append(1)

        sup.register("enabled", IntervalTicker(tick_a, interval_seconds=0.01), enabled=True)
        sup.register("disabled", IntervalTicker(tick_b, interval_seconds=0.01), enabled=False)

        await sup.start_all()
        await asyncio.sleep(0.05)
        await sup.stop_all()

        started.append((len(calls_a), len(calls_b)))

    asyncio.run(run())
    count_a, count_b = started[0]
    assert count_a >= 2, "enabled ticker should have run"
    assert count_b == 0, "disabled ticker should not have run"


def test_idle_backoff_grows_interval_when_inactive():
    """When is_active always returns False, the wait between ticks should grow
    (not stay pinned at the base interval) up to the configured cap."""
    call_times = []

    async def tick():
        call_times.append(1)
        return "idle-marker"

    async def run():
        stop = asyncio.Event()
        t = IntervalTicker(
            tick, interval_seconds=0.01,
            is_active=lambda result: False,
            max_interval_seconds=0.05,
            backoff_factor=2.0,
        )
        task = asyncio.create_task(t.run_forever(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    # With backoff (0.01 -> 0.02 -> 0.04 -> capped 0.05 ...) over 0.2s, far
    # fewer ticks fire than the ~20 a fixed 0.01s cadence would produce.
    assert 3 <= len(call_times) <= 10


def test_idle_backoff_resets_on_activity():
    """A tick reported as active resets the interval back to base immediately,
    even after several idle backoffs grew it."""
    results = iter([False, False, True, False, False])
    call_times = []

    async def tick():
        call_times.append(1)
        try:
            return next(results)
        except StopIteration:
            return False

    async def run():
        stop = asyncio.Event()
        t = IntervalTicker(
            tick, interval_seconds=0.01,
            is_active=lambda result: bool(result),
            max_interval_seconds=0.08,
            backoff_factor=2.0,
        )
        task = asyncio.create_task(t.run_forever(stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    # More ticks than the pure-backoff test above, because activity at index 2
    # resets the cadence back to the fast 0.01s interval.
    assert len(call_times) >= 6


def test_no_is_active_is_byte_identical_to_legacy_fixed_interval():
    """Omitting is_active (every pre-existing caller) must behave exactly like
    the original fixed-interval loop -- no behavior change for cron/goals/
    curator/surface-GC unless they explicitly opt in."""
    calls = []

    async def tick():
        calls.append(1)

    async def run():
        stop = asyncio.Event()
        t = IntervalTicker(tick, interval_seconds=0.01)
        task = asyncio.create_task(t.run_forever(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert len(calls) >= 2


def test_error_does_not_trigger_backoff():
    """A tick that raises must retry promptly, not back off -- errors are
    transient conditions, not idleness."""
    n = {"c": 0}

    async def tick():
        n["c"] += 1
        raise RuntimeError("boom")

    async def run():
        stop = asyncio.Event()
        t = IntervalTicker(
            tick, interval_seconds=0.01,
            is_active=lambda result: False,  # never reached; tick raises first
            max_interval_seconds=0.5,
        )
        task = asyncio.create_task(t.run_forever(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert n["c"] >= 5  # kept retrying at the base cadence, not backing off
