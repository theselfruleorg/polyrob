"""Regression (P1 finalization): board.py used `logger` in _append_attempt's
except block but never imported it, so a transient DB error during circuit-breaker
failure recording raised NameError instead of failing open as documented.
"""
import agents.task.goals.board as board_mod
from agents.task.goals.board import GoalBoard


def test_append_attempt_failopen_on_db_error(tmp_path, monkeypatch):
    b = GoalBoard(str(tmp_path / "goals.db"))
    gid = b.create(user_id="u1", title="do a thing")

    # Force the ledger UPDATE (inside _append_attempt) to raise, exercising the
    # fail-open except path that previously NameError'd on `logger`.
    real = board_mod.execute_retry

    def _boom(db_path, sql, params=()):
        if sql.strip().upper().startswith("UPDATE GOALS SET PAYLOAD"):
            raise RuntimeError("simulated db lock")
        return real(db_path, sql, params)

    monkeypatch.setattr(board_mod, "execute_retry", _boom)
    # Must NOT raise (fail-open); previously raised NameError: name 'logger'.
    b._append_attempt(gid, error="boom", session_id="s1")
