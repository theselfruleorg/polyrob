"""031 T10: the owner-only autonomy_control action."""
import pathlib

import pytest


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model, description)
            return fn
        return deco


class _Controller:
    def __init__(self, data_dir, tainted=False):
        self.registry = _Registry()
        self.container = type("C", (), {"config": type("Cfg", (), {"data_dir": data_dir})()})()
        self.user_id = "rob"
        self._is_sub_agent = False
        self.orchestrator = type("O", (), {"is_correspondent_tainted": lambda self: tainted})()


def _ctx(**meta):
    return type("Ctx", (), {"user_id": "rob", "role": "orchestrator", "is_sub_agent": False,
                            "session_id": "s", "metadata": meta})()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    monkeypatch.setattr("tools.controller.turn_origin._is_forged_or_autonomous_turn",
                        lambda *a: False)
    return tmp_path


def _action(c):
    from tools.controller.autonomy_control_action import register_autonomy_control_action
    register_autonomy_control_action(c)
    fn, model, desc = c.registry.actions["autonomy_control"]
    assert "stop" in desc.lower()
    return fn, model


@pytest.mark.asyncio
async def test_owner_turn_pauses_and_quotes_verified_state(home):
    c = _Controller(str(home))
    fn, model = _action(c)
    res = await fn(model(action="pause", scopes=["trading"], duration_minutes=60,
                         reason="owner said"), _ctx())
    assert res.error is None and res.extracted_content.startswith("⏸ Paused trading")
    from core.autonomy_control import read_state
    st = read_state(str(home))
    assert st.via == "agent" and st.reason == "owner said" and st.scopes == ("trading",)
    res = await fn(model(action="status"), _ctx())
    assert res.extracted_content.startswith("⏸ PAUSED (trading)")  # the shared headline
    assert "autonomy_control(resume)" in res.extracted_content   # the agent's own verb, not /resume
    res = await fn(model(action="resume"), _ctx())
    assert res.extracted_content.startswith("▶ Autonomy RESUMED")
    assert not read_state(str(home)).paused


@pytest.mark.asyncio
async def test_forged_turn_is_refused(home, monkeypatch):
    monkeypatch.setattr("tools.controller.turn_origin._is_forged_or_autonomous_turn",
                        lambda *a: True)
    c = _Controller(str(home))
    fn, model = _action(c)
    res = await fn(model(action="pause"), _ctx(turn_kind="self_wake"))
    assert res.error and "owner-only" in res.error
    from core.autonomy_control import read_state
    assert read_state(str(home)).paused is False


@pytest.mark.asyncio
async def test_non_owner_is_refused(home, monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "someone_else")
    monkeypatch.setattr("core.config_policy.local_mode_enabled", lambda: False)
    c = _Controller(str(home))
    fn, model = _action(c)
    res = await fn(model(action="pause"), _ctx())
    assert res.error and "owner-only" in res.error


def test_tainted_session_is_blocked_by_the_capability_gate():
    """A correspondent-tainted session never reaches the action at all (WS-A
    high-impact list) — neither pause nor resume from third-party data."""
    from agents.task.agent.core.correspondent_gate import _HIGH_IMPACT_NAMES
    assert "autonomy_control" in _HIGH_IMPACT_NAMES


@pytest.mark.asyncio
async def test_cancel_goals_cancels_ready_rows(home):
    from agents.task.goals.board import GoalBoard
    b = GoalBoard(str(home / "goals.db"))
    g = b.create(user_id="rob", title="held")
    c = _Controller(str(home))
    fn, model = _action(c)
    res = await fn(model(action="pause", cancel_goals=True), _ctx())
    assert "Cancelled 1 held goal row(s)" in res.extracted_content
    assert b.get(g.id).status == "cancelled"


def test_module_has_no_future_annotations():
    """The registry introspects the closure's first-param annotation; a stringized
    annotation breaks param-model routing (GLM live-test bug 2026-06-20)."""
    import ast
    src = pathlib.Path("tools/controller/autonomy_control_action.py").read_text(encoding="utf-8")
    futures = [n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    assert futures == []


def test_registered_on_the_real_controller_and_excluded_for_leaves():
    from tools.controller.delegation import LEAF, delegation_exclusions_for_child
    assert "autonomy_control" in delegation_exclusions_for_child(LEAF)
    from agents.task.agent.core.correspondent_gate import _HIGH_IMPACT_NAMES
    assert "autonomy_control" in _HIGH_IMPACT_NAMES
    src = pathlib.Path("tools/controller/service.py").read_text(encoding="utf-8")
    assert "register_autonomy_control_action(self)" in src
