"""WS-8: posture-gated goal/cron toolsets.

At AGENT_COMPUTE_POSTURE>=1 an autonomous goal/cron run with no explicit
payload.tools is provisioned with the compute toolset (code_execution + shell,
plus coding) so a self-env / build task can actually run — the acceptance goal
8632a4571b36 is exactly such a run. At posture 0 the defaults are byte-identical
(filesystem, task). CHILD_INHERITABLE_TOOLS gains code_execution/shell only at
posture>=1, resolved at call time (module import stays byte-identical).
"""
import pytest

import agents.task.constants as c
from agents.task.goals import dispatcher as d


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
    c._refreeze_compute_posture_for_tests()
    yield
    # See test_compute_posture.py: clear the env BEFORE refreezing — post-yield
    # runs before monkeypatch.undo, so a still-set posture would freeze and leak.
    import os
    os.environ.pop("AGENT_COMPUTE_POSTURE", None)
    c._refreeze_compute_posture_for_tests()


def _posture(monkeypatch, v):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", v)
    c._refreeze_compute_posture_for_tests()


def test_default_goal_tools_posture_0_unchanged():
    assert d.default_goal_tools() == ["filesystem", "task"]


def test_default_goal_tools_posture_1_adds_compute(monkeypatch):
    _posture(monkeypatch, "1")
    tools = d.default_goal_tools()
    assert "filesystem" in tools and "task" in tools
    assert "code_execution" in tools and "shell" in tools and "coding" in tools


def test_child_inheritable_posture_0_has_no_exec(monkeypatch):
    tools = d.child_inheritable_tools()
    assert "code_execution" not in tools and "shell" not in tools
    # the frozen module constant is never mutated
    assert "code_execution" not in d.CHILD_INHERITABLE_TOOLS


def test_child_inheritable_posture_1_adds_exec(monkeypatch):
    _posture(monkeypatch, "1")
    tools = d.child_inheritable_tools()
    assert "code_execution" in tools and "shell" in tools


class _Goal:
    def __init__(self, payload=None, parent_id=None, uid="rob"):
        self.payload = payload or {}
        self.parent_id = parent_id
        self.user_id = uid
        self.id = "g1"


class _Board:
    def __init__(self, parent=None):
        self._parent = parent

    def get(self, gid):
        return self._parent


def _dispatcher(board):
    disp = object.__new__(d.GoalDispatcher)
    disp.board = board
    return disp


def test_resolve_tools_posture_1_default(monkeypatch):
    _posture(monkeypatch, "1")
    disp = _dispatcher(_Board())
    tools = disp._resolve_tools(_Goal())
    assert "code_execution" in tools and "shell" in tools


def test_resolve_tools_posture_0_default(monkeypatch):
    disp = _dispatcher(_Board())
    assert disp._resolve_tools(_Goal()) == ["filesystem", "task"]


def test_resolve_tools_explicit_payload_always_wins(monkeypatch):
    _posture(monkeypatch, "1")
    disp = _dispatcher(_Board())
    assert disp._resolve_tools(_Goal(payload={"tools": ["filesystem"]})) == ["filesystem"]


def test_child_inherits_compute_at_posture_1(monkeypatch):
    _posture(monkeypatch, "1")
    parent = _Goal(payload={"tools": ["filesystem", "code_execution", "shell", "x402_pay"]})
    disp = _dispatcher(_Board(parent=parent))
    child = _Goal(parent_id="p1")
    inherited = disp._resolve_tools(child)
    assert "code_execution" in inherited and "shell" in inherited
    assert "x402_pay" not in inherited  # money tools never inherited


def test_cron_default_tools_posture_1(monkeypatch):
    _posture(monkeypatch, "1")
    from cron.runner import default_cron_tools
    tools = default_cron_tools()
    assert "code_execution" in tools and "shell" in tools


def test_cron_default_tools_posture_0(monkeypatch):
    from cron.runner import default_cron_tools
    assert default_cron_tools() == ["filesystem", "task"]
