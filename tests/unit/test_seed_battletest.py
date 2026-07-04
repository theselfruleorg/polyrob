"""Tests for the battle-test goal generator (scripts/seed_battletest.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.seed_battletest import (  # noqa: E402
    BUILD_TOOLS,
    CATEGORIES,
    RESEARCH_TOOLS,
    build_battletest_goals,
    build_build_goal,
    build_research_goal,
)

MODELS = ["deepseek/deepseek-v3.2", "minimax/minimax-m3", "x-ai/grok-4.3"]


def test_build_goal_payload_pins_model_and_tools():
    g = build_build_goal(category="strings", model="minimax/minimax-m3", index=0)
    assert g["payload"]["provider"] == "openrouter"
    assert g["payload"]["model"] == "minimax/minimax-m3"
    assert g["payload"]["tools"] == BUILD_TOOLS
    assert "skip_memory" not in g["payload"]  # ME-D2: dead knob retired from the payload
    assert g["payload"]["max_steps"] >= 8
    assert "strings" in g["body"].lower()
    assert "INDEX.md" in g["body"]
    assert "duplicate" in g["body"].lower()


def test_research_goal_uses_research_tools_and_readonly():
    g = build_research_goal(model="deepseek/deepseek-v3.2", index=0)
    assert g["payload"]["model"] == "deepseek/deepseek-v3.2"
    assert "anysite" in g["payload"]["tools"]
    assert set(g["payload"]["tools"]).issubset(set(RESEARCH_TOOLS))
    # no money / trading / social tools ever
    for forbidden in ("twitter", "hyperliquid", "polymarket", "x402", "wallet", "code_execution"):
        assert forbidden not in g["payload"]["tools"]


def test_round_robin_assigns_models_evenly():
    goals = build_battletest_goals(models=MODELS, per_model=4, research_per_model=1)
    build_goals = [g for g in goals if g["_kind"] == "build"]
    assert len(build_goals) == 4 * len(MODELS)
    counts = {m: 0 for m in MODELS}
    for g in build_goals:
        counts[g["payload"]["model"]] += 1
    assert all(c == 4 for c in counts.values())  # perfectly even


def test_research_goals_one_per_model():
    goals = build_battletest_goals(models=MODELS, per_model=4, research_per_model=1)
    research = [g for g in goals if g["_kind"] == "research"]
    assert len(research) == len(MODELS)
    assert {g["payload"]["model"] for g in research} == set(MODELS)


def test_categories_cycle_and_titles_unique():
    goals = build_battletest_goals(models=MODELS, per_model=4, research_per_model=0)
    # every build goal references a known category
    for g in goals:
        assert any(c in g["body"].lower() for c in CATEGORIES)
    titles = [g["title"] for g in goals]
    assert len(titles) == len(set(titles))  # unique titles -> distinct goal rows


def test_emit_json_is_seed_goal_compatible():
    """The emitted JSON must match scripts/seed_goal.py --from-json shape."""
    goals = build_battletest_goals(models=MODELS, per_model=2, research_per_model=1)
    # strip internal _kind marker -> the on-disk JSON
    payload = [{k: v for k, v in g.items() if not k.startswith("_")} for g in goals]
    s = json.dumps(payload)
    reloaded = json.loads(s)
    for g in reloaded:
        assert set(("title", "body", "priority", "payload")).issubset(g.keys())
        assert isinstance(g["payload"]["tools"], list)
