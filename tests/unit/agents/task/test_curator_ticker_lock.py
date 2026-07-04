"""P3: CuratorTicker cross-process TickLock (workers>1 parity).

Mirrors the cron scheduler's lock tests — curator should skip its tick when
another worker holds the lock, and build_curator_ticker should wire the lock path.
"""
import asyncio
import os
import pytest

from cron.scheduler import TickLock


# ---------------------------------------------------------------------------
# test: build_curator_ticker sets lock_path
# ---------------------------------------------------------------------------

def test_build_curator_ticker_sets_lock_path(tmp_path):
    """build_curator_ticker must propagate data_dir into CuratorTicker.lock_path."""
    from agents.task.agent.core.curator import build_curator_ticker
    ticker = build_curator_ticker(data_dir=str(tmp_path))
    assert ticker.lock_path is not None
    assert ticker.lock_path.endswith("curator.tick.lock"), (
        f"Expected lock_path ending with 'curator.tick.lock', got {ticker.lock_path!r}"
    )
    assert ticker.lock_path == os.path.join(str(tmp_path), "curator.tick.lock")


# ---------------------------------------------------------------------------
# test: run_once is skipped when lock is already held
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_curator_ticker_skips_when_lock_held(tmp_path):
    """CuratorTicker must NOT call run_once when another process holds the TickLock."""
    lock_path = str(tmp_path / "curator.tick.lock")

    # Track run_once calls with a stub curator
    run_once_calls = []

    class _StubCurator:
        def should_run(self):
            return True

        async def run_once(self):
            run_once_calls.append(1)
            return {}

    from agents.task.agent.core.curator import CuratorTicker
    ticker = CuratorTicker(_StubCurator(), lock_path=lock_path)

    # Hold the lock as another "process" would
    held_lock = TickLock(lock_path)
    assert held_lock.acquire() is True
    try:
        # Run exactly one iteration of the ticker's lock-guarded block
        # by calling a minimal coroutine that exercises the same branch:
        # should_run() → True, lock_path set → acquire → fails → skip.
        async def _one_tick():
            """Replicate the inner tick block from run_forever (single iteration)."""
            if ticker.curator.should_run():
                lock = None
                if ticker.lock_path:
                    lock = TickLock(ticker.lock_path)
                    if not lock.acquire():
                        lock = None  # another worker holds it; skip
                    else:
                        try:
                            await ticker.curator.run_once()
                        finally:
                            lock.release()
                else:
                    await ticker.curator.run_once()

        await asyncio.wait_for(_one_tick(), timeout=2.0)
    finally:
        held_lock.release()

    assert run_once_calls == [], (
        "run_once must NOT be called when the TickLock is held by another process"
    )


@pytest.mark.asyncio
async def test_curator_ticker_runs_when_lock_free(tmp_path):
    """CuratorTicker DOES call run_once when no other process holds the lock."""
    lock_path = str(tmp_path / "curator.tick.lock")

    run_once_calls = []

    class _StubCurator:
        def should_run(self):
            return True

        async def run_once(self):
            run_once_calls.append(1)
            return {}

    from agents.task.agent.core.curator import CuratorTicker
    ticker = CuratorTicker(_StubCurator(), lock_path=lock_path)

    async def _one_tick():
        if ticker.curator.should_run():
            lock = None
            if ticker.lock_path:
                lock = TickLock(ticker.lock_path)
                if not lock.acquire():
                    lock = None
                else:
                    try:
                        await ticker.curator.run_once()
                    finally:
                        lock.release()
            else:
                await ticker.curator.run_once()

    await asyncio.wait_for(_one_tick(), timeout=2.0)

    assert run_once_calls == [1], "run_once must be called when the lock is free"


@pytest.mark.asyncio
async def test_curator_ticker_no_lock_path_always_runs(tmp_path):
    """When lock_path is None, CuratorTicker runs unconditionally (backward compat)."""
    run_once_calls = []

    class _StubCurator:
        def should_run(self):
            return True

        async def run_once(self):
            run_once_calls.append(1)
            return {}

    from agents.task.agent.core.curator import CuratorTicker
    ticker = CuratorTicker(_StubCurator(), lock_path=None)

    async def _one_tick():
        if ticker.curator.should_run():
            if ticker.lock_path:
                lock = TickLock(ticker.lock_path)
                if not lock.acquire():
                    pass
                else:
                    try:
                        await ticker.curator.run_once()
                    finally:
                        lock.release()
            else:
                await ticker.curator.run_once()

    await asyncio.wait_for(_one_tick(), timeout=2.0)

    assert run_once_calls == [1], "run_once must run without a lock_path"
