"""``TickLock`` lives in the core tier (``core.file_lock``): the curator, the goal
dispatcher and the retention sweep take it without importing ``cron.scheduler``."""
import ast
import pathlib

from core.file_lock import TickLock

REPO = pathlib.Path(__file__).resolve().parents[3]


def test_tick_lock_is_non_blocking_and_exclusive(tmp_path):
    p = str(tmp_path / "tick.lock")
    a, b = TickLock(p), TickLock(p)
    assert a.acquire() is True
    assert a.acquire() is False  # one object is one hold
    assert b.acquire() is False  # held: returns False at once, never waits
    a.release()
    assert b.acquire() is True
    b.release()


def test_no_tick_lock_reader_imports_the_cron_scheduler():
    for rel in ("agents/task/agent/core/curator.py", "agents/task/goals/dispatcher.py",
                "polyrob/app/autonomy_runtime.py"):
        tree = ast.parse((REPO / rel).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "cron.scheduler" not in mods, rel
