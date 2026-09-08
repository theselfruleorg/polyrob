"""FIX 2 — the stream seeder must hold a cross-process lock.

`stream_is_due()` -> `seed_stream()` is a read-then-write with nothing between
them, and the seeder is deployed as an hourly systemd timer AND documented for
manual invocation (`python scripts/seed_streams.py --stream treasury-trading`).
Two overlapping runs could both observe `due == True` before either wrote, and
both seed — doubling the cycle that carries the money verb, which is exactly what
`max_live_goals` exists to prevent. The cron tick and the goal dispatcher both
take a `TickLock`; the seeder took nothing.

Reuses `cron.scheduler.TickLock` (never a second lock mechanism), scoped to the
data dir so two data homes do not block each other.
"""
import importlib.util
import os
from pathlib import Path

import pytest
import yaml

from agents.task.goals import streams as S
from agents.task.goals.board import GoalBoard
from cron.scheduler import TickLock

ROOT = Path(__file__).resolve().parents[5]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "seed_streams_script", ROOT / "scripts" / "seed_streams.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stream(sid: str) -> dict:
    return {"id": sid, "cadence_hours": 4,
            "objective": {"title": f"{sid} objective", "priority": 1},
            "goals": [{"title": f"{sid} leg", "body": "do the work",
                       "priority": 2, "tools": ["filesystem", "task"]}]}


@pytest.fixture
def manifest(tmp_path):
    p = tmp_path / "streams.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "streams": [_stream("alpha")]}),
                 encoding="utf-8")
    return p


def test_a_second_seeder_exits_cleanly_and_seeds_nothing(tmp_path, manifest, capsys):
    """The race: a manual run against the hourly timer. The loser writes NOTHING."""
    script = _load_script()
    db = str(tmp_path / "goals.db")

    held = TickLock(script.seeder_lock_path(db))
    assert held.acquire(), "test setup: the first seeder takes the lock"
    try:
        rc = script.main(["--db", db, "--user-id", "rob", "--manifest", str(manifest)])
    finally:
        held.release()

    assert rc == 0, "a locked-out seeder is not an error — another seeder is doing the work"
    out = capsys.readouterr().out.lower()
    assert "another seeder" in out or "lock" in out
    board = GoalBoard(db)
    assert S.stream_live_goals(board, "rob", "alpha") == 0, \
        "the second seeder must not double-seed the cycle"


def test_the_lock_is_released_so_the_next_run_can_seed(tmp_path, manifest):
    script = _load_script()
    db = str(tmp_path / "goals.db")

    assert script.main(["--db", db, "--user-id", "rob", "--manifest", str(manifest)]) == 0
    assert not os.path.exists(script.seeder_lock_path(db)), \
        "the seeder must release its lock (the next hourly tick needs it)"
    board = GoalBoard(db)
    assert S.stream_live_goals(board, "rob", "alpha") == 1


def test_lock_is_scoped_to_the_data_dir(tmp_path):
    script = _load_script()
    a = script.seeder_lock_path(str(tmp_path / "homeA" / "goals.db"))
    b = script.seeder_lock_path(str(tmp_path / "homeB" / "goals.db"))
    assert a != b, "two data homes must not block each other"


def test_dry_run_reports_even_while_a_seeder_holds_the_lock(tmp_path, manifest, capsys):
    """A dry run writes nothing, so it never needs the lock."""
    script = _load_script()
    db = str(tmp_path / "goals.db")
    held = TickLock(script.seeder_lock_path(db))
    assert held.acquire()
    try:
        rc = script.main(["--db", db, "--user-id", "rob",
                          "--manifest", str(manifest), "--dry-run"])
    finally:
        held.release()
    assert rc == 0
    assert "[alpha]" in capsys.readouterr().out
