"""P0-4 (2026-07-09): board-stall guard + autonomy kill-switch.

Observed prod stall: 0 ready goals + blocked ones → planner says "queue healthy,
nothing to add" → instance idle 14h. The planner must instead force NEW achievable
work. Plus an owner kill-switch to halt all autonomous spend/dispatch.
"""
from agents.task.constants import AutonomyConfig
from agents.task.goals.board import GoalBoard
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


# --- T2.1 Task 4: waiting-on-a-live-dependency is not a stall --------------

def test_stalled_board_suppressed_when_waiting_goal_has_live_dependency(tmp_path):
    board = GoalBoard(str(tmp_path / "g.db"))
    board.create_objective(user_id="rob", title="mission")
    prereq = board.create(user_id="rob", title="prerequisite work entirely")
    board.create(user_id="rob", title="dependent work entirely", depends_on=[prereq.id])
    # a blocked goal too, proving the guard isn't just "no blocked goals"
    doomed = board.create(user_id="rob", title="doomed work entirely", max_retries=1)
    board.claim(doomed.id, "w", ttl_seconds=60)
    board.record_failure(doomed.id, error="boom")

    p = build_planner_prompt(board, "rob", None)
    assert "STALLED BOARD" not in p
    assert "WAITING ON DEPENDENCIES" in p
    assert "dependent work entirely" in p


def test_stalled_board_still_fires_without_waiting_goals(tmp_path):
    board = GoalBoard(str(tmp_path / "g2.db"))
    board.create_objective(user_id="rob", title="mission")
    doomed = board.create(user_id="rob", title="doomed work alone", max_retries=1)
    board.claim(doomed.id, "w", ttl_seconds=60)
    board.record_failure(doomed.id, error="boom")

    p = build_planner_prompt(board, "rob", None)
    assert "STALLED BOARD" in p


# --- T2.1 review (Critical): waiting-existence is NOT liveness --------------
#
# `block_from_ready` (agent-declared OUTCOME: BLOCKED) flips a 'ready'
# prerequisite straight to 'blocked' WITHOUT board.py's dep_failed cascade —
# deliberately, since an agent-declared block is owner-recoverable and
# cascading it would force a double owner-unblock. So a dependent can sit in
# 'waiting' on a genuinely dead prerequisite forever; treating any waiting
# goal as proof the board is fine reproduces the documented 14h-idle stall.

def test_case_a_stalled_present_when_waiting_prereq_is_agent_blocked(tmp_path):
    """(a) waiting dependent whose prerequisite was block_from_ready-blocked
    -> STALLED clause PRESENT (not suppressed)."""
    board = GoalBoard(str(tmp_path / "g3.db"))
    board.create_objective(user_id="rob", title="mission")
    prereq = board.create(user_id="rob", title="prerequisite that goes agent-blocked",
                          max_retries=3)
    board.claim(prereq.id, "w", ttl_seconds=60)
    board.record_failure(prereq.id, error="transient")  # 1 < max_retries=3 -> back to 'ready'
    assert board.get(prereq.id).status == "ready"
    assert board.block_from_ready(prereq.id, error="agent declared BLOCKED: needs creds") is True
    assert board.get(prereq.id).status == "blocked"  # NOT cascaded to its dependent

    dependent = board.create(user_id="rob", title="dependent stuck behind agent block",
                             depends_on=[prereq.id])
    assert dependent.status == "waiting", "block_from_ready never cascades dep_failed"

    p = build_planner_prompt(board, "rob", None)
    assert "STALLED BOARD" in p, (
        "a waiting goal behind a block_from_ready'd (agent-declared BLOCKED) "
        "prerequisite is DEAD, not live — must not silently suppress the stall banner")


def test_case_b_stalled_suppressed_when_waiting_prereq_is_running(tmp_path):
    """(b) waiting dependent with a running prerequisite -> suppressed."""
    board = GoalBoard(str(tmp_path / "g4.db"))
    board.create_objective(user_id="rob", title="mission")
    prereq = board.create(user_id="rob", title="prerequisite currently running")
    board.claim(prereq.id, "w", ttl_seconds=900)
    assert board.get(prereq.id).status == "running"
    dependent = board.create(user_id="rob", title="dependent waiting on running prereq",
                             depends_on=[prereq.id])
    assert dependent.status == "waiting"
    doomed = board.create(user_id="rob", title="unrelated doomed goal", max_retries=1)
    board.claim(doomed.id, "w", ttl_seconds=60)
    board.record_failure(doomed.id, error="boom")

    p = build_planner_prompt(board, "rob", None)
    assert "STALLED BOARD" not in p


def test_case_c_stalled_suppressed_for_waiting_on_waiting_chain_with_live_root(tmp_path):
    """(c) waiting-on-waiting chain with a live root -> suppressed."""
    board = GoalBoard(str(tmp_path / "g5.db"))
    board.create_objective(user_id="rob", title="mission")
    root = board.create(user_id="rob", title="root prerequisite still ready")
    mid = board.create(user_id="rob", title="middle goal waiting on root", depends_on=[root.id],
                       force=True)
    assert mid.status == "waiting"
    leaf = board.create(user_id="rob", title="leaf goal waiting on middle", depends_on=[mid.id],
                        force=True)
    assert leaf.status == "waiting"
    doomed = board.create(user_id="rob", title="unrelated doomed goal two", max_retries=1)
    board.claim(doomed.id, "w", ttl_seconds=60)
    board.record_failure(doomed.id, error="boom")

    p = build_planner_prompt(board, "rob", None)
    assert "STALLED BOARD" not in p


def test_stalled_suppression_is_boardwide_one_live_chain_covers_a_dead_one(tmp_path):
    """The guard mirrors the pre-existing board-wide 0-ready/blocked semantics
    (not a per-goal one): ANY live waiting goal means the board genuinely has
    work in flight, so STALLED stays suppressed even while a SEPARATE dead
    waiting chain (behind an agent-blocked prereq) also sits on the board."""
    board = GoalBoard(str(tmp_path / "g6.db"))
    board.create_objective(user_id="rob", title="mission")
    # dead chain
    dead_prereq = board.create(user_id="rob", title="dead prereq", max_retries=3)
    board.claim(dead_prereq.id, "w", ttl_seconds=60)
    board.record_failure(dead_prereq.id, error="transient")
    board.block_from_ready(dead_prereq.id, error="agent declared BLOCKED: needs creds")
    board.create(user_id="rob", title="dead dependent", depends_on=[dead_prereq.id])
    # live chain
    live_prereq = board.create(user_id="rob", title="live prereq still ready")
    board.create(user_id="rob", title="live dependent", depends_on=[live_prereq.id])

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
