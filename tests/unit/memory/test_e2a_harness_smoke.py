"""Smoke test: E2a cross-phase recall harness returns valid metrics."""
from data.livetest.memory.e2a_crossphase import run_e2a, _DEMO_SNAPSHOTS, _DEMO_GOLD


def test_e2a_runs_and_returns_lexical_recall():
    res = run_e2a(_DEMO_SNAPSHOTS, _DEMO_GOLD)
    assert "lexical" in res and "recall_at_3" in res["lexical"]
    assert 0.0 <= res["lexical"]["recall_at_3"] <= 1.0
    assert "embeddings" in res
    assert "decision_rule" in res
    assert isinstance(res["lexical"]["n"], int) and res["lexical"]["n"] > 0
