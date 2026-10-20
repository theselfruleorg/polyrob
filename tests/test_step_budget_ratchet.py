"""083 WS-4 — no tool argument sets a step budget.

The operator owns the budget; the model owns the work. Before 083 the model
picked its own step budget in four places (``goal_create.max_steps``,
``delegate_task.max_steps`` and ``tasks[].max_steps``, ``worker_manage
propose max_steps``, the console work form) — and set ``max_steps: 10`` on the
two prod goals that then failed twice each. One operator cap,
``AUTONOMOUS_MAX_STEPS``, now applies to goal, cron, delegation and worker runs.

This ratchet reads the committed prompt surface (``tests/golden/prompt_surface``,
kept current by ``tests/test_prompt_surface_golden.py``) and the flag-gated
worker schema the golden does not render: no tool schema may carry a property
named ``max_steps`` at any depth.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_TOOLS = ROOT / "tests" / "golden" / "prompt_surface" / "tools"
FORBIDDEN = "max_steps"


def _paths_with_forbidden_property(node, path="$"):
    """Every JSON path under *node* where a ``properties`` map names the key."""
    hits = []
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and FORBIDDEN in props:
            hits.append(f"{path}.properties.{FORBIDDEN}")
        for key, value in node.items():
            hits.extend(_paths_with_forbidden_property(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits.extend(_paths_with_forbidden_property(value, f"{path}[{i}]"))
    return hits


def test_the_golden_tool_surface_exists():
    assert sorted(GOLDEN_TOOLS.glob("*.json")), "the prompt-surface golden is missing"


def test_no_rendered_tool_schema_offers_a_step_budget():
    offenders = []
    for path in sorted(GOLDEN_TOOLS.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        offenders += [f"{path.name}: {p}" for p in _paths_with_forbidden_property(schema)]
    assert not offenders, (
        "a tool schema offers the model a step budget — the cap is the operator's "
        "AUTONOMOUS_MAX_STEPS (083):\n  " + "\n  ".join(offenders))


def test_the_step_budget_tools_carry_no_field():
    """The four former model-settable budgets, read from their param models
    directly (``worker_manage`` is flag-gated and absent from the golden)."""
    from tools.controller.views import DelegateTaskAction, SubtaskAction
    from tools.controller.worker_manage_action import WorkerManageAction
    from tools.goal_tools import GoalCreateAction
    for model in (GoalCreateAction, DelegateTaskAction, SubtaskAction, WorkerManageAction):
        assert not _paths_with_forbidden_property(model.model_json_schema()), model.__name__
