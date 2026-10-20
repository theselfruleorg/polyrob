"""2026-10-14 review D-11 / E-6 (owner decision D6) — the owner spend view.

No chat seat showed AI cost by model, kind of work, job or day; `/usage` was
REPL-only and one chat. `compute_economics` now splits by model, purpose, day,
kind of work and job, with $ per useful run, and `spend_view_lines` is the one
render every seat prints. No new store: usage_records + episodes.
"""
import json
import os
import sqlite3
import time

import pytest

from core import status_economics as E


def _row(sid, cost, model="m1", purpose="agent_step", day="2026-10-13 10:00:00",
         inp=1000, cached=800, **md):
    return {"session_id": sid, "input_tokens": inp, "cached_tokens": cached,
            "output_tokens": 10, "api_cost_usd": cost, "timestamp": day,
            "metadata": json.dumps({"model": model, "purpose": purpose, **md})}


RUNS = {"c1": {"kind": "cron", "job": "job:aaa", "useful": False},
        "c2": {"kind": "cron", "job": "job:aaa", "useful": True},
        "g1": {"kind": "goal", "job": "goal:bbb", "useful": True},
        "o1": {"kind": "chat", "job": None, "useful": False}}


def test_splits_and_cost_per_useful_run():
    rows = [_row("c1", 0.30), _row("c2", 0.10, billed_cost_usd=0.10),
            _row("g1", 0.20, model="m2", day="2026-10-14 01:00:00"),
            _row("o1", 0.40, purpose="compaction"), _row("x9", 0.05, regime="room")]
    e = E.compute_economics(rows, 7 * 86400, runs=RUNS)
    assert [b["key"] for b in e["by_model"]] == ["m1", "m2"]
    assert e["by_model"][0]["cost_usd"] == pytest.approx(0.85)
    assert {b["key"] for b in e["by_purpose"]} == {"agent_step", "compaction"}
    assert [b["key"] for b in e["by_day"]] == ["2026-10-13", "2026-10-14"]
    regimes = {b["key"]: b["cost_usd"] for b in e["by_regime"]}
    assert regimes == {"cron": pytest.approx(0.40), "goal": pytest.approx(0.20),
                       "chat": pytest.approx(0.40), "room": pytest.approx(0.05)}
    jobs = {b["key"]: b["cost_usd"] for b in e["by_job"]}
    assert jobs == {"job:aaa": pytest.approx(0.40), "goal:bbb": pytest.approx(0.20)}
    assert e["runs"] == 3 and e["useful_runs"] == 2
    assert e["cost_per_useful_run_usd"] == pytest.approx(0.30)
    assert e["noop_run_cost_usd"] == pytest.approx(0.30)
    assert e["billed_share"] == pytest.approx(0.2)
    assert e["by_model"][0]["cache_ratio"] == pytest.approx(0.8)


def test_without_a_run_index_the_job_split_is_unknown_not_empty():
    e = E.compute_economics([_row("c1", 0.1)], 86400)
    assert e["by_job"] is None and e["runs"] is None and e["by_regime"] is None


def test_episode_useful_matches_did_something():
    from agents.task.agent.core.episodic_digest import did_something
    for facts in ({}, {"sent": ["x"]}, {"outputs": ["post: y"]}, {"tx": ["0xab"]}):
        stored = {k: v for k, v in facts.items() if v}
        assert E.episode_useful(json.dumps(stored)) == did_something(
            {"sent": [], "outputs": [], "tx": [], **facts})


def _home(tmp_path, monkeypatch, usage, episodes):
    db = tmp_path / "database" / "bot.db"
    os.makedirs(db.parent, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage_records (id INTEGER PRIMARY KEY, user_id TEXT, "
                "session_id TEXT, input_tokens INTEGER, cached_tokens INTEGER, "
                "output_tokens INTEGER, api_cost_usd REAL, metadata TEXT, timestamp TEXT)")
    for sid, cost in usage:
        con.execute("INSERT INTO usage_records (user_id, session_id, input_tokens, "
                    "cached_tokens, output_tokens, api_cost_usd, metadata, timestamp) "
                    "VALUES ('rob', ?, 100, 50, 5, ?, '{\"model\": \"m1\"}', "
                    "strftime('%Y-%m-%d %H:%M:%S','now'))", (sid, cost))
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(db))
    if episodes is not None:
        con = sqlite3.connect(tmp_path / "memory.db")
        con.execute("CREATE TABLE episodes (ts INTEGER, user_id TEXT, session_id TEXT, "
                    "kind TEXT, goal_id TEXT, meta TEXT)")
        for sid, kind, meta in episodes:
            con.execute("INSERT INTO episodes VALUES (?,?,?,?,NULL,?)",
                        (int(time.time()), "rob", sid, kind, json.dumps(meta)))
        con.commit()
        con.close()


def test_spend_view_end_to_end_and_render(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, [("s1", 0.5), ("s2", 0.25)],
          [("s1", "cron", {"job_id": "abcdef1234", "sent": ["hi"]}),
           ("s2", "cron", {"job_id": "abcdef1234"})])
    view = E.spend_view("rob", str(tmp_path), now=time.time(), days=7)
    lines = E.spend_view_lines(view, names={"job:abcdef1234": "EXIT CHECK"})
    text = "\n".join(lines)
    assert "AI cost, the last 7 days: $0.75" in text
    assert "EXIT CHECK: $0.75 (100%)" in text
    assert "2 scheduled-job and goal runs, 1 did something · $0.75 per useful run" in text
    assert "by model:" in text and "by day:" in text
    line = E.top_cost_line(view, names={"job:abcdef1234": "EXIT CHECK"})
    assert line.startswith("Top AI cost: EXIT CHECK $0.75 (100%) of $0.75")


def test_unreadable_run_index_is_named_not_zero(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, [("s1", 0.5)], None)
    (tmp_path / "memory.db").write_text("not a database")
    view = E.spend_view("rob", str(tmp_path), now=time.time(), days=1)
    text = "\n".join(E.spend_view_lines(view))
    assert "could not read the run records" in text and "not zero" in text


def test_no_ledger_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "nope" / "bot.db"))
    view = E.spend_view("rob", str(tmp_path), now=time.time(), days=3)
    assert "no records yet" in E.spend_view_lines(view)[0]
