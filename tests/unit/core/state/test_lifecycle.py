"""098 §3.5: ResourceLifecycle."""
import asyncio

import pytest

from core.state.lifecycle import (
    CLOSED, DEGRADED, LIFECYCLE, NEW, READY, REMOVED, ResourceClosed, ResourceLifecycle,
    ResourceUnavailable,
)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Factory:
    def __init__(self, fail=0):
        self.fail = fail
        self.created = []
        self.closed = []

    async def create(self):
        if self.fail:
            self.fail -= 1
            raise ConnectionError("boom")
        h = object()
        self.created.append(h)
        return h

    def teardown(self, h):
        self.closed.append(h)


def _lc(f, clock, **kw):
    return ResourceLifecycle("test", create=f.create, teardown=f.teardown,
                             backoff_base=1.0, backoff_cap=8.0, clock=clock, **kw)


def test_table_is_well_formed():
    assert LIFECYCLE.validate() == []


def test_start_is_idempotent_and_stop_tears_down():
    async def go():
        f, c = Factory(), Clock()
        lc = _lc(f, c)
        h1 = await lc.start()
        h2 = await lc.start()
        assert h1 is h2 and lc.state == READY and len(f.created) == 1
        await lc.stop()
        await lc.stop()
        assert lc.state == CLOSED and f.closed == [h1]
        with pytest.raises(ResourceClosed):
            await lc.ensure_ready()
        # closed is restartable
        h3 = await lc.start()
        assert lc.state == READY and h3 is not h1
        await lc.stop(remove=True)
        assert lc.state == REMOVED
        with pytest.raises(ResourceClosed):
            await lc.start()
    asyncio.run(go())


def test_failed_start_degrades_with_capped_backoff_and_no_attempt_ceiling():
    async def go():
        f, c = Factory(fail=6), Clock()
        lc = _lc(f, c)
        delays = []
        for _ in range(6):
            with pytest.raises(ResourceUnavailable):
                await lc.ensure_ready()
            assert lc.state == DEGRADED
            delays.append(lc.retry_in())
            # before the backoff elapses, no re-create runs
            with pytest.raises(ResourceUnavailable):
                await lc.ensure_ready()
            c.t = lc.next_attempt_at
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
        h = await lc.ensure_ready()  # 7th try succeeds: no max attempts
        assert lc.state == READY and lc.attempts == 0 and h is f.created[-1]
    asyncio.run(go())


def test_probe_failure_tears_down_before_degraded_then_recreates():
    async def go():
        f, c = Factory(), Clock()
        healthy = {"ok": True}
        lc = _lc(f, c, probe=lambda h: healthy["ok"])
        h1 = await lc.start()
        assert await lc.probe() is True
        healthy["ok"] = False
        assert await lc.probe() is False
        assert lc.state == DEGRADED and f.closed == [h1] and lc.handle is None
        healthy["ok"] = True
        assert await lc.probe() is False  # backoff not elapsed
        c.t += 1.0
        assert await lc.probe() is True
        assert lc.state == READY and lc.handle is not h1
    asyncio.run(go())


def test_fail_and_restart_never_leave_a_live_handle_unclosed():
    async def go():
        f, c = Factory(), Clock()
        lc = _lc(f, c)
        h1 = await lc.start()
        h2 = await lc.restart()
        assert f.closed == [h1] and h2 is not h1 and lc.state == READY
        await lc.fail(RuntimeError("drop"))
        assert f.closed == [h1, h2] and lc.state == DEGRADED
        assert isinstance(lc.last_error, RuntimeError)
    asyncio.run(go())


def test_concurrent_ensure_ready_creates_once():
    async def go():
        f, c = Factory(), Clock()
        slow = f.create

        async def create():
            await asyncio.sleep(0.01)
            return await slow()
        lc = ResourceLifecycle("test", create=create, teardown=f.teardown, clock=c)
        hs = await asyncio.gather(*[lc.ensure_ready() for _ in range(5)])
        assert len(f.created) == 1 and all(h is hs[0] for h in hs)
    asyncio.run(go())


def test_cancelled_start_leaves_degraded_not_starting():
    async def go():
        c = Clock()

        async def create():
            await asyncio.sleep(10)
        lc = ResourceLifecycle("test", create=create, clock=c)
        t = asyncio.create_task(lc.start())
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert lc.state == DEGRADED
    asyncio.run(go())


def test_view_and_adopt():
    async def go():
        f, c = Factory(), Clock()
        lc = _lc(f, c)
        m = {READY: "connected", DEGRADED: "error"}
        assert lc.state == NEW and lc.view(m, "disconnected") == "disconnected"
        lc.adopt("h")
        assert lc.view(m) == "connected" and await lc.ensure_ready() == "h"
        await lc.stop()
        assert f.closed == ["h"]
    asyncio.run(go())


def test_cancelled_stop_still_ends_closed_and_can_start_again():
    """098 review: a cancel during the teardown await left CLOSING, so the next
    start was the illegal edge CLOSING -> STARTING."""
    async def go():
        c = Clock()
        f = Factory()

        async def slow_teardown(h):
            await asyncio.sleep(10)
        lc = ResourceLifecycle("test", create=f.create, teardown=slow_teardown, clock=c)
        await lc.start()
        t = asyncio.create_task(lc.stop())
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert lc.state == CLOSED
        await lc.start()                      # strict mode: raises on an illegal edge
        assert lc.state == READY
    asyncio.run(go())
