"""B-T1 — cron ticker factory for the app lifespan.

`build_cron_ticker` wires CronJobStore + make_agent_runner + CronScheduler into a
CronTicker the FastAPI lifespan can start/stop. The factory is pure (no FastAPI), so
it is unit-tested directly. Lifespan wiring stays gated behind CRON_ENABLED.
"""
import os

import pytest

from cron.runner import build_cron_ticker, CronTicker


class _FakeTaskAgent:
    async def create_session(self, **kwargs):
        return {"id": "x"}


def test_factory_returns_ticker_wired_to_store(tmp_path):
    ticker = build_cron_ticker(_FakeTaskAgent(), data_dir=str(tmp_path), interval_seconds=30)
    assert isinstance(ticker, CronTicker)
    assert ticker.interval_seconds == 30
    assert ticker.scheduler.store.db_path == os.path.join(str(tmp_path), "cron.db")
    assert ticker.scheduler.lock_path == os.path.join(str(tmp_path), "cron.tick.lock")


@pytest.mark.asyncio
async def test_tick_once_on_empty_store_runs_nothing(tmp_path):
    ticker = build_cron_ticker(_FakeTaskAgent(), data_dir=str(tmp_path))
    result = await ticker.tick_once()
    assert result.ran == []
    assert result.failed == []
