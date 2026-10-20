"""2026-10-14 D6 — `/usage [days]` is an every-seat owner verb.

It was REPL-only and showed one chat. Telegram (and the console, which routes
through it) and the terminal now print the same spend view
(`core.status_economics.spend_view_lines`), with jobs named by their task.
"""
import json
import os
import sqlite3
import time

from core import verbs
from core.surfaces.dispatcher import _COMMANDS
from surfaces.telegram import usage_ops
from surfaces.telegram.admin_verbs import ADMIN_VERBS
from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS


def test_usage_is_an_every_seat_owner_verb():
    row = verbs.verb_for("/usage")
    assert row is not None and not row.seats
    assert verbs.verb_for("/cost") is row
    assert "/usage" in _COMMANDS and "/usage" in _OWNER_ADMIN_COMMANDS
    assert "/usage" in ADMIN_VERBS


def test_parse_days():
    assert usage_ops.parse_days([]) == 7
    assert usage_ops.parse_days(["30"]) == 30 and usage_ops.parse_days(["3d"]) == 3
    assert usage_ops.parse_days(["500"]) == 90
    assert usage_ops.parse_days(["soon"]) is None and usage_ops.parse_days(["0"]) is None


def test_bad_days_return_the_usage_line(tmp_path):
    assert usage_ops.usage_reply("rob", str(tmp_path), ["x", "y"]) == usage_ops.USAGE


def test_reply_names_the_job_by_its_task(tmp_path, monkeypatch):
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService
    job = CronService(CronJobStore(cron_db_path(str(tmp_path)))).schedule(
        task="EXIT CHECK — sell on a stop", schedule_spec="1h", user_id="rob")
    db = tmp_path / "database" / "bot.db"
    os.makedirs(db.parent, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage_records (id INTEGER PRIMARY KEY, user_id TEXT, "
                "session_id TEXT, input_tokens INTEGER, cached_tokens INTEGER, "
                "output_tokens INTEGER, api_cost_usd REAL, metadata TEXT, timestamp TEXT)")
    con.execute("INSERT INTO usage_records (user_id, session_id, input_tokens, cached_tokens, "
                "output_tokens, api_cost_usd, metadata, timestamp) VALUES "
                "('rob','s1',100,50,5,0.42,'{}',strftime('%Y-%m-%d %H:%M:%S','now'))")
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(db))
    con = sqlite3.connect(tmp_path / "memory.db")
    con.execute("CREATE TABLE episodes (ts INTEGER, user_id TEXT, session_id TEXT, "
                "kind TEXT, goal_id TEXT, meta TEXT)")
    con.execute("INSERT INTO episodes VALUES (?,?,?,?,NULL,?)",
                (int(time.time()), "rob", "s1", "cron", json.dumps({"job_id": job.id})))
    con.commit()
    con.close()
    text = usage_ops.usage_reply("rob", str(tmp_path), ["1"])
    assert "AI cost, the last 24 h: $0.42" in text
    assert "EXIT CHECK — sell on a stop: $0.42" in text


def test_an_unreadable_ledger_is_not_zero(tmp_path, monkeypatch):
    bad = tmp_path / "bot.db"
    bad.write_text("not sqlite")
    monkeypatch.setenv("DB_PATH", str(bad))
    text = usage_ops.usage_reply("rob", str(tmp_path), [])
    assert "could not read the records" in text and "not a zero" in text
