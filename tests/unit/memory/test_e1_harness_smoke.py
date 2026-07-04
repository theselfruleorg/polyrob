"""Smoke test: E1 cross-session recall harness returns valid metrics."""
import asyncio

from data.livetest.memory.e1_recall import run_e1, _DEMO_CORPUS, _DEMO_GOLD


def test_e1_runs_and_returns_metrics():
    res = asyncio.run(run_e1(_DEMO_CORPUS, _DEMO_GOLD))
    assert "fts5" in res and "recall_at_5" in res["fts5"]
    assert 0.0 <= res["fts5"]["recall_at_5"] <= 1.0
    assert "hybrid" in res
    assert "decision_rule" in res
    assert isinstance(res["fts5"]["n"], int) and res["fts5"]["n"] > 0
