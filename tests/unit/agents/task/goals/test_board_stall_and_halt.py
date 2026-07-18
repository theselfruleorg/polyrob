"""P0-4 (2026-07-09): board-stall guard + autonomy kill-switch.

Observed prod stall: 0 ready goals + blocked ones → planner says "queue healthy,
nothing to add" → instance idle 14h. The planner must instead force NEW achievable
work. Plus an owner kill-switch to halt all autonomous spend/dispatch.
"""
from agents.task.constants import AutonomyConfig
from agents.task.goals.planner import build_planner_prompt


class _G:
    def __init__(self, title, status, kind="goal", err=None):
        self.title = title
        self.status = status
        self.kind = kind
        self.last_failure_error = err
        self.id = "x"
        self.payload = {}
        self.completed_at = 0
        self.body = ""


class _Board:
    def __init__(self, goals, objs):
        self._g = goals
        self._o = objs

    def objectives(self, user_id, status):
        return self._o

    def list(self, user_id, status, limit=20):
        return [g for g in self._g if g.status == status]


def test_stalled_board_forbids_queue_healthy():
    board = _Board([_G("earn thing", "blocked", err="agent declared BLOCKED")],
                   [_G("mission", "active", kind="objective")])
    p = build_planner_prompt(board, "rob", None)
    assert "STALLED BOARD" in p
    assert "queue healthy" in p.lower() and "do not" in p.lower()


def test_healthy_board_has_no_stall_directive():
    board = _Board([_G("do x", "ready")], [_G("mission", "active", kind="objective")])
    p = build_planner_prompt(board, "rob", None)
    assert "STALLED BOARD" not in p


def test_autonomy_halted_env(monkeypatch):
    monkeypatch.setenv("AUTONOMY_HALT", "true")
    assert AutonomyConfig.autonomy_halted() is True
    monkeypatch.setenv("AUTONOMY_HALT", "false")
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    assert AutonomyConfig.autonomy_halted() is False


def test_autonomy_halted_file(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    assert AutonomyConfig.autonomy_halted() is False
    (tmp_path / "AUTONOMY_HALT").write_text("")
    assert AutonomyConfig.autonomy_halted() is True
