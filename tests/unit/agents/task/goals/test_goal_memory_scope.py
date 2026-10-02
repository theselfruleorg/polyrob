"""025 — the goal side of memory scopes: root label, regime clamp, promotion."""
import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals import memory_scope as GM
from modules.memory import scope as S

USER = "tenant_g"


@pytest.fixture()
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("MEMORY_SCOPES_ENABLED", "true")
    monkeypatch.delenv("AUTONOMY_MEMORY_REGIME", raising=False)


def _dag(board):
    obj = board.create_objective(user_id=USER, title="Standing objective")
    root = board.create(user_id=USER, title="Investigate the pricing page", parent_id=obj.id)
    child = board.create(user_id=USER, title="Collect three competitor prices",
                         parent_id=root.id, force=True)
    return obj, root, child


def test_off_adds_nothing(board, monkeypatch):
    monkeypatch.delenv("MEMORY_SCOPES_ENABLED", raising=False)
    _obj, root, _child = _dag(board)
    assert GM.goal_request_fields(board, root) == {}


def test_a_dag_shares_the_root_label_and_objectives_are_not_scopes(board, on):
    _obj, root, child = _dag(board)
    assert GM.root_goal(board, child).id == root.id
    assert GM.root_goal(board, root).id == root.id  # the objective parent is not a scope
    want = {"memory_scope": f"goal:{root.id}", "memory_regime": "scoped"}
    assert GM.goal_request_fields(board, root) == want
    assert GM.goal_request_fields(board, child) == want


def test_a_child_is_reclamped_to_its_root(board, on):
    root = board.create(user_id=USER, title="Sealed investigation",
                        payload={"memory_regime": "sealed"})
    child = board.create(user_id=USER, title="Try to escape to shared", parent_id=root.id,
                         payload={"memory_regime": "shared"}, force=True)
    assert GM.goal_regime(board, child) == "sealed"


def test_a_shared_goal_runs_unscoped(board, on):
    g = board.create(user_id=USER, title="Plain shared goal", payload={"memory_regime": "shared"})
    assert GM.goal_request_fields(board, g) == {}


class _Prov:
    is_external = True

    def __init__(self, n=3):
        self.calls, self.n = [], n

    def promote_scope(self, user_id, label):
        self.calls.append((user_id, label))
        return self.n


@pytest.fixture()
def prov():
    from modules.memory import registry as R
    R.reset_memory_registry()
    p = _Prov()
    R.set_external_memory_provider(p)
    yield p
    R.reset_memory_registry()


def test_promotion_only_for_a_verified_root(board, on, prov):
    _obj, root, child = _dag(board)
    assert GM.promote_after_success(board, child, verified="verified") == 0
    assert GM.promote_after_success(board, root, verified="unverified") == 0
    assert prov.calls == []
    assert GM.promote_after_success(board, root, verified="verified") == 3
    assert prov.calls == [(USER, f"goal:{root.id}")]
    kinds = [e["kind"] for e in board.events(root.id)]
    assert "memory_promoted" in kinds


def test_promotion_is_off_while_scopes_are_off(board, prov, monkeypatch):
    monkeypatch.delenv("MEMORY_SCOPES_ENABLED", raising=False)
    g = board.create(user_id=USER, title="Any goal at all")
    assert GM.promote_after_success(board, g, verified="verified") == 0
    assert prov.calls == []


def test_promotion_line():
    assert GM.promotion_line(0) is None
    assert GM.promotion_line(1) == "Promoted 1 memory to shared recall."
    assert GM.promotion_line(4) == "Promoted 4 memories to shared recall."


def test_session_request_carries_and_binds_the_scope(on):
    from agents.task.task_agent_support import (SessionRequest, bind_memory_scope,
                                                memory_scope_fields)
    fields = memory_scope_fields({"task": "t", "goal_id": "g1",
                                  "memory_scope": "goal:g1", "memory_regime": "scoped"})
    req = SessionRequest(task="t", provider="openai", model="gpt-5", **fields)
    assert (req.goal_id, req.memory_scope, req.memory_regime) == ("g1", "goal:g1", "scoped")
    bind_memory_scope("sess-1", req)
    assert S.session_scope("sess-1") == S.MemoryScopeSpec("goal:g1", "scoped")
    # the recreate path reads the persisted request dict
    bind_memory_scope("sess-2", dict(req.__dict__))
    assert S.session_scope("sess-2") == S.MemoryScopeSpec("goal:g1", "scoped")
    # chat carries none -> unbound (shared)
    bind_memory_scope("sess-3", SessionRequest(task="hi", provider="openai", model="gpt-5"))
    assert S.session_scope("sess-3") is None
