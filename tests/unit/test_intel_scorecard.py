"""Tests for the intelligence-loop scorecard metrics (scripts/intel_scorecard.py).

These cover the *pure* aggregation + flag logic — the deterministic, $0 core the
light intel tick runs every ~2.5h. All time is injected (`now=`) so windowing is
deterministic; no DB or network is touched.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.intel_scorecard import (  # noqa: E402
    evaluate_flags,
    render_row,
    summarize_episodes,
    summarize_goals,
)

HOUR = 3600
NOW = 1_000_000  # arbitrary fixed epoch for deterministic windowing


def _ep(kind, outcome="done", steps=5, spend=0.0, artifacts="[]", ts_ago_h=1.0):
    return {
        "kind": kind,
        "outcome": outcome,
        "steps": steps,
        "spend_usd": spend,
        "artifacts": artifacts,
        "ts": NOW - int(ts_ago_h * HOUR),
        "user_id": "rob",
    }


# --------------------------------------------------------------- summarize_episodes


def test_episodes_windowing_excludes_old_rows():
    rows = [
        _ep("cron", ts_ago_h=1),
        _ep("cron", ts_ago_h=48),  # outside a 24h window
    ]
    s = summarize_episodes(rows, now=NOW, window_hours=24)
    assert s["total"] == 1
    assert s["by_kind"]["cron"] == 1


def test_episodes_noop_ratio_counts_low_step_artifactless_autonomous():
    rows = [
        _ep("cron", steps=4, artifacts="[]"),          # no-op heartbeat
        _ep("self_wake", steps=1, artifacts="[]"),     # no-op heartbeat
        _ep("goal", steps=20, artifacts='["a.md"]'),   # real work (artifact)
        _ep("cron", steps=30, artifacts="[]"),         # high-step -> not a no-op
    ]
    s = summarize_episodes(rows, now=NOW, window_hours=24, noop_step_floor=6)
    assert s["autonomous_total"] == 4
    assert s["noop"] == 2
    assert s["with_artifacts"] == 1
    assert abs(s["noop_ratio"] - 0.5) < 1e-9


def test_episodes_artifacts_parse_list_and_json_and_empty():
    rows = [
        _ep("goal", artifacts='["x.md","y.md"]'),
        _ep("goal", artifacts=["z.md"]),  # already a list
        _ep("goal", artifacts=""),        # empty string -> no artifact
        _ep("goal", artifacts="[]"),      # empty list -> no artifact
    ]
    s = summarize_episodes(rows, now=NOW, window_hours=24)
    assert s["with_artifacts"] == 2


def test_episodes_chat_is_not_autonomous():
    rows = [_ep("chat", steps=2, artifacts="[]"), _ep("cron", steps=2, artifacts="[]")]
    s = summarize_episodes(rows, now=NOW, window_hours=24)
    assert s["autonomous_total"] == 1  # chat excluded
    assert s["noop"] == 1


def test_episodes_spend_and_steps_aggregate():
    rows = [_ep("goal", steps=10, spend=0.02), _ep("cron", steps=5, spend=0.01)]
    s = summarize_episodes(rows, now=NOW, window_hours=24)
    assert abs(s["spend_usd"] - 0.03) < 1e-9
    assert s["steps_total"] == 15


def test_episodes_empty_is_safe():
    s = summarize_episodes([], now=NOW, window_hours=24)
    assert s["total"] == 0 and s["autonomous_total"] == 0 and s["noop_ratio"] == 0.0


# ------------------------------------------------------------------ summarize_goals


def _g(status, kind="goal", cf=0, err=None, title="t"):
    return {
        "status": status,
        "kind": kind,
        "consecutive_failures": cf,
        "last_failure_error": err,
        "title": title,
        "id": "g1",
    }


def test_goals_status_and_board_depth():
    rows = [
        _g("ready"), _g("running"), _g("triage"),
        _g("blocked", cf=3, err="boom"),
        _g("done"), _g("done"),
        _g("active", kind="objective", title="mission"),
    ]
    s = summarize_goals(rows)
    assert s["by_status"]["done"] == 2
    assert s["ready"] == 1 and s["running"] == 1 and s["triage"] == 1
    assert s["active"] == 3  # ready+running+triage
    assert s["blocked"] == 1
    assert s["objectives_active"] == 1
    assert s["blocked_list"][0]["last_failure_error"] == "boom"


def test_goals_objective_only_when_active():
    rows = [_g("done", kind="objective"), _g("active", kind="objective")]
    s = summarize_goals(rows)
    assert s["objectives_active"] == 1


def test_goals_llm_exhausted_counts_only_live_marker_rows():
    """015 #3: count non-terminal goals whose latest failure carries the
    llm_provider_exhausted marker; terminal rows (recovered) and objectives don't."""
    rows = [
        _g("ready", err="llm_provider_exhausted: Session failed: ... 402"),
        _g("blocked", err="llm_provider_exhausted: ALL LLM PROVIDERS EXHAUSTED"),
        _g("done", err="llm_provider_exhausted: recovered later"),   # terminal -> excluded
        _g("ready", err="run did not complete (refusal or empty)"),  # no marker
        _g("active", kind="objective", err="llm_provider_exhausted: x"),  # objective -> excluded
    ]
    s = summarize_goals(rows)
    assert s["llm_exhausted"] == 2


# -------------------------------------------------------------------- evaluate_flags


def test_flag_high_noop_trips_at_threshold():
    eps = {"autonomous_total": 10, "noop": 8, "noop_ratio": 0.8, "with_artifacts": 1}
    goals = {"active": 3, "objectives_active": 1}
    f = evaluate_flags(eps, goals, noop_threshold=0.70)
    assert f["high_noop"] is True
    assert f["any"] is True


def test_flag_board_stalled_when_no_active_goals():
    eps = {"autonomous_total": 2, "noop": 0, "noop_ratio": 0.0, "with_artifacts": 2}
    goals = {"active": 0, "objectives_active": 1}
    f = evaluate_flags(eps, goals)
    assert f["board_stalled"] is True
    assert f["any"] is True


def test_flag_no_output_when_autonomous_but_zero_artifacts():
    eps = {"autonomous_total": 5, "noop": 1, "noop_ratio": 0.2, "with_artifacts": 0}
    goals = {"active": 4, "objectives_active": 1}
    f = evaluate_flags(eps, goals)
    assert f["no_output"] is True


def test_flag_objective_missing():
    eps = {"autonomous_total": 5, "noop": 1, "noop_ratio": 0.2, "with_artifacts": 3}
    goals = {"active": 4, "objectives_active": 0}
    f = evaluate_flags(eps, goals)
    assert f["objective_missing"] is True


def test_flags_all_clear():
    eps = {"autonomous_total": 5, "noop": 1, "noop_ratio": 0.2, "with_artifacts": 3}
    goals = {"active": 4, "objectives_active": 1}
    f = evaluate_flags(eps, goals)
    assert f["any"] is False


def test_flag_llm_provider_exhausted_trips_and_absent_key_is_safe():
    eps = {"autonomous_total": 5, "noop": 1, "noop_ratio": 0.2, "with_artifacts": 3}
    f = evaluate_flags(eps, {"active": 4, "objectives_active": 1, "llm_exhausted": 1})
    assert f["llm_provider_exhausted"] is True
    assert f["any"] is True
    # a legacy summary without the key must stay safe (flag off)
    f2 = evaluate_flags(eps, {"active": 4, "objectives_active": 1})
    assert f2["llm_provider_exhausted"] is False


def test_flags_safe_when_no_autonomous_activity():
    # A totally idle window should not fire high_noop/no_output (guards divide-by-zero
    # and avoids screaming "no output" when nothing ran on purpose).
    eps = {"autonomous_total": 0, "noop": 0, "noop_ratio": 0.0, "with_artifacts": 0}
    goals = {"active": 0, "objectives_active": 1}
    f = evaluate_flags(eps, goals)
    assert f["high_noop"] is False
    assert f["no_output"] is False
    # board_stalled still legitimately fires (active==0)
    assert f["board_stalled"] is True


# ----------------------------------------------------------------------- render_row


def test_render_row_is_single_markdown_row_with_delta():
    eps = {"total": 12, "autonomous_total": 10, "noop": 7, "noop_ratio": 0.70,
           "with_artifacts": 2, "spend_usd": 0.05, "steps_total": 80,
           "by_kind": {"cron": 7, "goal": 3}}
    goals = {"active": 2, "ready": 1, "running": 1, "blocked": 1,
             "objectives_active": 1}
    flags = {"high_noop": True, "board_stalled": False, "no_output": False,
             "objective_missing": False, "any": True}
    row = render_row(ts_label="2026-07-08 12:00Z", episodes=eps, goals=goals,
                     flags=flags, prev={"noop_ratio": 0.50})
    assert row.startswith("|")
    assert row.count("|") >= 8  # a real table row
    assert "\n" not in row       # exactly one row
    assert "high_noop" in row    # the tripped flag is named
    assert "2026-07-08 12:00Z" in row
    # delta vs prev (0.70 - 0.50 = +0.20) is surfaced
    assert "+0.20" in row or "+20" in row


# ------------------------------------------------------------ bare-script invocability


def test_runs_as_bare_script_without_repo_root_on_syspath(tmp_path):
    """Regression: the intel loop invokes this as `python3 scripts/intel_scorecard.py`
    from the repo root, which puts `scripts/` (not the repo root) at sys.path[0]. A
    2026-07-16 sweep briefly added a module-level `from core.runtime_paths import ...`
    to compute the --memory-db/--goals-db argparse defaults, which crashed under exactly
    that invocation (`core` package unreachable) — see the module docstring's
    "dependency-light... works with system python3 outside the venv" contract.
    """
    memory_db = tmp_path / "memory.db"
    goals_db = tmp_path / "goals.db"
    for db, table in ((memory_db, "episodes"), (goals_db, "goals")):
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute(f"CREATE TABLE {table} (id INTEGER)")
        conn.commit()
        conn.close()

    result = subprocess.run(
        [sys.executable, "scripts/intel_scorecard.py",
         "--memory-db", str(memory_db), "--goals-db", str(goals_db),
         "--row", "--label", "test"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert result.stdout.startswith("|")
