"""098 CL-16: the HALF_OPEN slot is returned on every exit."""
import asyncio
import time

import pytest

from utils.circuit_breaker import CIRCUIT, CircuitBreaker, CircuitBreakerError, CircuitState


def _half_open(**kw):
    b = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=60,
                       half_open_max_calls=3, **kw)
    b.state = CircuitState.HALF_OPEN
    b._half_open_since = time.time()
    return b


def test_table_is_well_formed():
    assert CIRCUIT.validate() == []


def test_cancelled_call_returns_half_open_slot():
    async def go():
        b = _half_open()
        gate = asyncio.Event()

        async def hang():
            await gate.wait()
        tasks = [asyncio.create_task(b.call(hang)) for _ in range(3)]
        await asyncio.sleep(0.01)
        assert b._half_open_calls == 3
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert b._half_open_calls == 0
        assert b.stats.failed_calls == 0  # a cancel is not a failure

        async def ok():
            return 1
        assert await b.call(ok) == 1  # a fourth call is admitted
    asyncio.run(go())


def test_unexpected_exception_returns_slot():
    async def go():
        b = _half_open(expected_exception=ValueError)

        async def boom():
            raise KeyError("x")
        for _ in range(5):
            with pytest.raises(KeyError):
                await b.call(boom)
        assert b._half_open_calls == 0 and b.state == CircuitState.HALF_OPEN
    asyncio.run(go())


def test_held_slots_past_recovery_window_reopen_probing():
    async def go():
        b = _half_open()
        b._half_open_calls = 3
        b._half_open_since = time.time() - 61

        async def ok():
            return 1
        assert await b.call(ok) == 1
    asyncio.run(go())


def test_state_changes_bounded():
    async def go():
        b = CircuitBreaker(name="t", failure_threshold=1, recovery_timeout=0)

        async def boom():
            raise RuntimeError("x")
        for _ in range(200):
            with pytest.raises((RuntimeError, CircuitBreakerError)):
                await b.call(boom)
        assert len(b.stats.state_changes) <= 50
        assert len(b.get_stats()["state_changes"]) == 10
    asyncio.run(go())


def test_concurrent_cancel_and_success_never_negative():
    async def go():
        b = _half_open(success_threshold=10)
        gate = asyncio.Event()

        async def wait_ok():
            await gate.wait()
            return 1
        tasks = [asyncio.create_task(b.call(wait_ok)) for _ in range(3)]
        await asyncio.sleep(0.01)
        tasks[0].cancel()
        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert b._half_open_calls == 0
    asyncio.run(go())


def test_slot_from_an_old_episode_is_not_returned_to_a_new_one():
    async def go():
        b = _half_open()
        gate = asyncio.Event()

        async def hang():
            await gate.wait()
        t = asyncio.create_task(b.call(hang))
        await asyncio.sleep(0.01)
        b._transition_to_open()
        b.stats.last_failure_time = 0
        b._transition_to_half_open()
        b._half_open_calls = 2  # two probes of the new episode in flight
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        assert b._half_open_calls == 2
    asyncio.run(go())
