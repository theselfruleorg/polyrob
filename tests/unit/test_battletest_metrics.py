"""Tests for the battle-test metrics digest (scripts/battletest_metrics.py)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.battletest_metrics import (  # noqa: E402
    budget_verdict,
    summarize_goals,
)


def _row(status, model, cf=0, started=None, created=100.0):
    return {
        "status": status,
        "payload": {"model": model} if model else {},
        "consecutive_failures": cf,
        "started_at": started,
        "created_at": created,
        "result": "ok",
    }


def test_summarize_counts_by_status_and_model():
    rows = [
        _row("done", "deepseek/deepseek-v3.2"),
        _row("done", "minimax/minimax-m3"),
        _row("running", "x-ai/grok-4.3"),
        _row("ready", "deepseek/deepseek-v3.2"),
        _row("blocked", "minimax/minimax-m3", cf=3),
    ]
    s = summarize_goals(rows)
    assert s["by_status"]["done"] == 2
    assert s["by_status"]["running"] == 1
    assert s["by_status"]["ready"] == 1
    assert s["by_status"]["blocked"] == 1
    assert s["total"] == 5
    # per-model done/total
    assert s["by_model"]["deepseek/deepseek-v3.2"]["total"] == 2
    assert s["by_model"]["deepseek/deepseek-v3.2"]["done"] == 1
    assert s["by_model"]["minimax/minimax-m3"]["blocked"] == 1
    assert s["blocked_total"] == 1


def test_summarize_handles_missing_model():
    rows = [_row("done", None)]
    s = summarize_goals(rows)
    assert s["by_model"]["(unpinned)"]["total"] == 1


def test_budget_verdict_ok_warn_stop():
    # baseline 16.31, budget 10 -> warn at >8 new (>24.31), stop at >10 new (>26.31)
    ok = budget_verdict(usage=20.0, baseline=16.31, budget=10.0)
    assert ok["level"] == "ok" and ok["new_spend"] < 8

    warn = budget_verdict(usage=25.0, baseline=16.31, budget=10.0)
    assert warn["level"] == "warn"

    stop = budget_verdict(usage=27.0, baseline=16.31, budget=10.0)
    assert stop["level"] == "stop"
    assert stop["should_stop"] is True


def test_budget_verdict_none_usage_is_unknown():
    v = budget_verdict(usage=None, baseline=16.31, budget=10.0)
    assert v["level"] == "unknown"
    assert v["should_stop"] is False
