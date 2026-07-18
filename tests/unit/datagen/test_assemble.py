"""Wave 1 Task 1 — canonical trajectory record + session assembler."""
import json
import sqlite3

import pytest

from datagen.assemble import (
    assemble_record,
    load_episode_labels,
    read_agent_steps,
    read_message_history,
    summarize_llm_usage,
)
from datagen.record import DEFAULT_LABELS, SCHEMA_VERSION, TrajectoryRecord


def _make_session_dir(tmp_path, *, legacy_history=False):
    """Build a minimal on-disk session dir mirroring production layout."""
    sdir = tmp_path / "sessions" / "sess-1"
    history_payload = {
        "session_id": "sess-1",
        "total_tokens": 42,
        "saved_at": "2026-07-11T10:00:00",
        "messages": [
            {"type": "HumanMessage", "content": "do the thing",
             "origin": "USER", "metadata": {"input_tokens": 5}},
            {"type": "ToolMessage", "content": "done ok",
             "tool_call_id": "c1", "metadata": {}},
        ],
    }
    if legacy_history:
        target = sdir / "message_history.json"
    else:
        target = sdir / "memory" / "message_history.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(history_payload))

    hist_dir = sdir / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    (hist_dir / "agent_history_main.json").write_text(json.dumps({
        "history": [{
            "model_output": {"current_state": {"next_goal": "g"},
                             "action": [{"done": {}}]},
            "result": [{"extracted_content": "ok", "error": None}],
            "state": {},
        }]
    }))

    usage_dir = sdir / "data" / "llm_usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    (usage_dir / "llm_usage_1.json").write_text(json.dumps({
        "token_count": 10, "cost_estimate": 0.01,
        "prompt_tokens": 8, "completion_tokens": 2,
    }))
    return sdir


def test_assemble_record_full_layout(tmp_path):
    sdir = _make_session_dir(tmp_path)
    rec = assemble_record(sdir, session_meta={"task": "do the thing",
                                              "model": "m1", "provider": "p1"})
    assert isinstance(rec, TrajectoryRecord)
    assert rec.session_id == "sess-1"
    assert len(rec.messages) == 2
    assert len(rec.steps) == 1
    assert rec.usage["records"] == 1
    assert rec.usage["total_tokens"] == 10
    assert rec.labels["outcome"] == "unknown"
    assert rec.task == "do the thing"
    assert rec.model == "m1"
    assert rec.created_at == "2026-07-11T10:00:00"
    assert rec.to_dict()["schema_version"] == SCHEMA_VERSION


def test_read_message_history_legacy_root_fallback(tmp_path):
    sdir = _make_session_dir(tmp_path, legacy_history=True)
    hist = read_message_history(sdir)
    assert len(hist["messages"]) == 2


def test_read_message_history_missing_returns_empty(tmp_path):
    assert read_message_history(tmp_path / "nope") == {}


def test_read_agent_steps_root_fallback(tmp_path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    (sdir / "agent_history_solo.json").write_text(json.dumps({
        "history": [{"model_output": None, "result": [], "state": {}}]
    }))
    assert len(read_agent_steps(sdir)) == 1


def test_summarize_llm_usage_none_when_absent(tmp_path):
    assert summarize_llm_usage(tmp_path) is None


def test_load_episode_labels(tmp_path):
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE episodes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts INTEGER NOT NULL, started_ts INTEGER, "
        "user_id TEXT NOT NULL, session_id TEXT NOT NULL, thread_key TEXT, "
        "kind TEXT NOT NULL, task TEXT, outcome TEXT, summary TEXT, "
        "artifacts TEXT NOT NULL DEFAULT '[]', spend_usd REAL NOT NULL DEFAULT 0, "
        "steps INTEGER NOT NULL DEFAULT 0, goal_id TEXT, "
        "surfaced INTEGER NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '{}', "
        "created_at INTEGER NOT NULL)")
    conn.execute(
        "INSERT INTO episodes (ts, user_id, session_id, kind, outcome, summary,"
        " spend_usd, steps, created_at) VALUES (1, 'u1', 'sess-1', 'goal',"
        " 'done', 'it worked', 0.5, 7, 1)")
    conn.commit()
    conn.close()

    labels = load_episode_labels(db, "u1", "sess-1")
    assert labels["outcome"] == "done"
    assert labels["spend_usd"] == 0.5
    assert labels["steps"] == 7


def test_load_episode_labels_missing_db_and_row(tmp_path):
    assert load_episode_labels(tmp_path / "none.db", "u", "s") is None
    db = tmp_path / "memory.db"
    sqlite3.connect(db).close()
    assert load_episode_labels(db, "u", "s") is None


def test_default_labels_are_complete():
    assert set(DEFAULT_LABELS) >= {"outcome", "verified", "refusal",
                                   "all_actions_errored", "steps", "spend_usd"}


def test_read_agent_steps_real_data_history_layout(tmp_path):
    """REGRESSION (finalization B-1): the live ledger is written by
    history_io via pm().get_history_dir() = <session>/data/history/ —
    NOT <session>/history/. A reader that misses this returns steps=[]
    for every real session (the highest-value training signal)."""
    import json as _json

    from datagen.assemble import read_agent_steps

    sdir = tmp_path / "sess"
    hist = sdir / "data" / "history"
    hist.mkdir(parents=True)
    (hist / "agent_history_task_agent.json").write_text(_json.dumps({
        "history": [{
            "model_output": {"current_state": {"next_goal": "g"},
                             "action": [{"done": {}}]},
            "result": [{"extracted_content": "ok", "error": None}],
        }]
    }))
    steps = read_agent_steps(sdir)
    assert len(steps) == 1
    assert steps[0]["model_output"]["current_state"]["next_goal"] == "g"
