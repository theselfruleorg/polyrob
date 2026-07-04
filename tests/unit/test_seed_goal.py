"""G4 — operator goal seeder (scripts/seed_goal.py).

Tests the pure helpers (build_payload / build_earn_money_goal) and the end-to-end
seed into a real GoalBoard, without invoking the CLI.
"""
import importlib.util
import os

_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "seed_goal.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("seed_goal", os.path.abspath(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_payload_defaults():
    m = _load()
    p = m.build_payload(tools=["filesystem", "browser"])
    assert p["tools"] == ["filesystem", "browser"]
    assert p["max_steps"] == 20
    assert "skip_memory" not in p  # ME-D2: dead knob retired from the payload
    assert p["provider"] == "anthropic"
    assert "model" not in p  # omitted when not given


def test_build_payload_with_model():
    m = _load()
    p = m.build_payload(tools=["task"], max_steps=15, provider="openai", model="gpt-5.5")
    assert p["max_steps"] == 15
    assert p["provider"] == "openai"
    assert p["model"] == "gpt-5.5"


def test_earn_money_goal_has_capable_tools():
    m = _load()
    g = m.build_earn_money_goal()
    assert g["title"]
    assert g["body"]
    assert g["priority"] == 1
    tools = g["payload"]["tools"]
    # the capable earn set — research/browse/code, no money/social/trading tools
    for t in ("browser", "anysite", "coding"):
        assert t in tools
    for forbidden in ("wallet", "x402", "hyperliquid", "polymarket", "twitter"):
        assert forbidden not in tools


def test_seed_earn_money_creates_row(tmp_path):
    m = _load()
    db = str(tmp_path / "goals.db")
    goal = m.seed_goal(db, m.build_earn_money_goal(), user_id="rob")
    from agents.task.goals.board import GoalBoard
    b = GoalBoard(db)
    got = b.get(goal.id)
    assert got is not None
    assert got.user_id == "rob"
    assert "anysite" in got.payload["tools"]
