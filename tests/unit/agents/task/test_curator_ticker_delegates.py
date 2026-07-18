"""Regression (P4 finalization): CuratorTicker.run_forever hand-rolled the exact
loop core/tickers.IntervalTicker exists to consolidate (CronTicker/GoalTicker already
delegate). It must delegate too — the curator-specific body lives in _tick_once.
"""
import inspect

from agents.task.agent.core.curator import CuratorTicker


def test_run_forever_delegates_to_interval_ticker():
    src = inspect.getsource(CuratorTicker.run_forever)
    assert "IntervalTicker" in src, "run_forever must delegate to the shared IntervalTicker"
    assert "while not" not in src, "run_forever must not hand-roll its own loop"


def test_tick_once_exists_and_is_async():
    assert inspect.iscoroutinefunction(CuratorTicker._tick_once)
