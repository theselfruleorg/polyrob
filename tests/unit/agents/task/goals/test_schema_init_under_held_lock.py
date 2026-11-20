"""A store opens while another process holds its write lock (proposal 125 S16, B-H3).

The schema setup ran ``BEGIN IMMEDIATE`` for an additive column check, so a second
``GoalBoard`` / ``CronJobStore`` waited on (then failed against) a held lock. The
``CREATE … IF NOT EXISTS`` script and an already-migrated column check need no lock.
"""
import sqlite3
import time

from modules.goals.board import GoalBoard
from cron.jobs import CronJobStore


def _hold(db):
    holder = sqlite3.connect(db, timeout=0.1)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_goal_board_opens_under_a_held_write_lock(tmp_path):
    db = str(tmp_path / "goals.db")
    GoalBoard(db)
    holder = _hold(db)
    try:
        t0 = time.monotonic()
        GoalBoard(db)
        assert time.monotonic() - t0 < 1.0
    finally:
        holder.rollback()
        holder.close()


def test_cron_store_opens_under_a_held_write_lock(tmp_path):
    db = str(tmp_path / "cron.db")
    CronJobStore(db)
    holder = _hold(db)
    try:
        t0 = time.monotonic()
        CronJobStore(db)
        assert time.monotonic() - t0 < 1.0
    finally:
        holder.rollback()
        holder.close()


def test_goal_board_adds_a_missing_column_to_an_old_table(tmp_path):
    db = str(tmp_path / "goals.db")
    GoalBoard(db)
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(goals)")}
    assert {"kind", "claim_token"} <= cols
