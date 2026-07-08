"""P1-6/7 (2026-07-06 UX handoff) — goal events read as titles + outcomes.

``goal 798bd1064b35 run`` was meaningless: no title, no outcome, and the
dispatcher's started/done pair rendered as two identical lines. Normalization
now enriches goal events with the goal's title via a small cached, fail-open
lookup into goals.db, and the summary carries outcome/status (+ failure
reason) so a start→done pair reads as such.
"""
import json
import sqlite3

import pytest

from webview import activity


GOAL_ID = "798bd1064b35"
TITLE = "Grow the X audience"


@pytest.fixture
def goals_db(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    db = tmp_path / "goals.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE goals (id TEXT PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO goals VALUES (?, ?)", (GOAL_ID, TITLE))
    con.commit()
    con.close()
    activity._GOAL_TITLE_CACHE.clear()
    yield db
    activity._GOAL_TITLE_CACHE.clear()


def _telemetry_row(outcome, **attrs):
    return {
        "id": 7,
        "ts": 1751800000.0,
        "kind": "goal_run",
        "user_id": "rob",
        "session_id": "",
        "source": "goal",
        "attrs": json.dumps({"goal_id": GOAL_ID, "outcome": outcome, **attrs}),
    }


def test_goal_run_summary_carries_title_and_outcome(goals_db):
    ev = activity.normalize_db_event("telemetry", _telemetry_row("started"))
    assert TITLE in ev["summary"]
    assert "started" in ev["summary"]


def test_started_and_done_read_differently(goals_db):
    started = activity.normalize_db_event("telemetry", _telemetry_row("started"))
    done = activity.normalize_db_event("telemetry", _telemetry_row("done"))
    assert started["summary"] != done["summary"]


def test_failed_outcome_includes_reason(goals_db):
    ev = activity.normalize_db_event(
        "telemetry", _telemetry_row("failed", reason="ran out of budget")
    )
    assert "failed" in ev["summary"]
    assert "ran out of budget" in ev["summary"]


def test_goal_source_row_enriched_with_title(goals_db):
    row = {
        "id": 3,
        "goal_id": GOAL_ID,
        "kind": "claimed",
        "payload": json.dumps({"worker": "w1"}),
        "created_at": 1751800001.0,
    }
    ev = activity.normalize_db_event("goal", row)
    assert TITLE in ev["summary"]
    assert "claimed" in ev["summary"]


def test_missing_goals_db_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "nowhere"))
    activity._GOAL_TITLE_CACHE.clear()
    ev = activity.normalize_db_event("telemetry", _telemetry_row("started"))
    assert GOAL_ID[:8] in ev["summary"]  # still identifies the goal
    assert "started" in ev["summary"]
    activity._GOAL_TITLE_CACHE.clear()


def test_title_lookup_is_cached(goals_db, monkeypatch):
    activity.normalize_db_event("telemetry", _telemetry_row("started"))
    assert activity._GOAL_TITLE_CACHE.get(GOAL_ID) == TITLE
    # Second normalization must not re-open the DB: poison the file path.
    monkeypatch.setenv("POLYROB_DATA_DIR", "/definitely/not/there")
    ev = activity.normalize_db_event("telemetry", _telemetry_row("done"))
    assert TITLE in ev["summary"]
