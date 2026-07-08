"""P4 — centralized sync->async bridge (persistent loop)."""
import asyncio

import pytest

from core.async_bridge import run_coroutine_sync


async def _double(x):
    await asyncio.sleep(0)
    return x * 2


def test_no_running_loop_uses_asyncio_run():
    # Called from a plain sync context (no event loop on this thread).
    assert run_coroutine_sync(_double(21)) == 42


@pytest.mark.asyncio
async def test_from_running_loop_uses_persistent_bridge():
    # Called synchronously from within a running loop (the Agent.__init__ case).
    assert run_coroutine_sync(_double(21)) == 42


@pytest.mark.asyncio
async def test_bridge_reuses_one_thread_across_calls():
    import threading
    before = {t.name for t in threading.enumerate()}
    for _ in range(5):
        assert run_coroutine_sync(_double(1)) == 2
    after = {t.name for t in threading.enumerate()}
    # at most one new persistent bridge thread, regardless of call count
    new_threads = [n for n in (after - before) if "async-bridge" in n]
    assert len(new_threads) <= 1


def test_timeout_raises():
    async def _slow():
        await asyncio.sleep(5)

    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        run_coroutine_sync(_slow(), timeout=0.1)


@pytest.mark.asyncio
async def test_exception_propagates_from_bridge():
    async def _boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        run_coroutine_sync(_boom())


def test_p2_7_worker_thread_uses_bridge_not_throwaway_loop():
    """P2-7: from a WORKER thread with no running loop, run_coroutine_sync must route
    through the persistent bridge loop (one live loop shared across calls), NOT a
    throwaway asyncio.run() loop per call — the latter breaks loop-bound httpx clients.

    Verified by asserting two sequential worker-thread calls run on the SAME loop.
    """
    import asyncio
    import threading

    seen_loops = []

    async def _capture():
        seen_loops.append(id(asyncio.get_running_loop()))
        return True

    results = []

    def _worker():
        # not the main thread, no running loop -> must use the bridge
        run_coroutine_sync(_capture())
        run_coroutine_sync(_capture())
        results.append("done")

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=10)

    assert results == ["done"]
    assert len(seen_loops) == 2
    assert seen_loops[0] == seen_loops[1], "worker-thread calls must share ONE bridge loop"
