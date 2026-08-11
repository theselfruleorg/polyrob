"""Three small defects that each silently did nothing (or the wrong thing).

1. A judge completion whose structured output failed to bind went UNBILLED — the
   early return jumped over the metering call, even though the provider had run and
   charged for the completion.
2. The `<environment>` block's "Tools loaded this session:" line has never rendered:
   it read `getattr(self, "tool_ids", None)` on the Agent, which has no such
   attribute (only SystemPrompt and MessageManager do), so the value was always None
   and the `if tool_ids:` guard never fired.
3. A browser-state exception set `state = None` while both sibling branches build a
   minimal BrowserState. Downstream consumers dereference it unconditionally, so the
   "don't fail - just continue" comment actually described an uncaught AttributeError
   escaping the whole run.
"""
import ast
import inspect

import pytest


# --- 1. judge metering -------------------------------------------------------

def test_both_judge_exits_meter_the_completion():
    """A successful invoke must be billed on BOTH exits: the normal verdict path and
    the schema-mismatch early return."""
    from agents.task.agent.core import output_validation

    src = inspect.getsource(output_validation.OutputValidationMixin._validate_output)
    assert src.count("_meter_judge_call") >= 2, (
        "only one exit meters — a judge call whose structured output failed to bind "
        "is a real, provider-charged completion and must not be free"
    )


def test_schema_mismatch_path_meters_before_returning():
    """Order matters: the meter must happen before the fail-open return."""
    from agents.task.agent.core import output_validation

    src = inspect.getsource(output_validation.OutputValidationMixin._validate_output)
    idx = src.find("no parsed verdict")
    assert idx > 0, "schema-mismatch branch not found — test needs updating"
    tail = src[idx:idx + 700]
    meter_at = tail.find("_meter_judge_call")
    return_at = tail.find("return True")
    assert meter_at != -1, "schema-mismatch branch does not meter"
    assert meter_at < return_at, "meter must run before the early return"


@pytest.mark.asyncio
async def test_meter_judge_call_is_fail_open():
    """Metering must never break validation."""
    from agents.task.agent.core.output_validation import OutputValidationMixin

    class _A(OutputValidationMixin):
        usage_tracker = None
        user_id = None
        session_id = ""
        agent_id = ""

    await _A()._meter_judge_call(object(), {"raw": "x"}, 0.1)


# --- 2. environment block tool list ------------------------------------------

def test_environment_block_gets_the_real_loaded_toolset():
    """Must read the controller (the actual source), not a non-existent Agent attr."""
    from agents.task.agent.core import construction

    tree = ast.parse(inspect.getsource(construction))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name != "build_environment_context":
            continue
        for kw in node.keywords:
            if kw.arg == "tool_ids":
                rendered = ast.unparse(kw.value)
                assert "list_tools" in rendered, (
                    f"tool_ids={rendered} — the Agent has no `tool_ids` attribute, so "
                    "the 'Tools loaded this session' line never renders"
                )
                found = True
    assert found, "build_environment_context call not found — test needs updating"


def test_agent_still_has_no_tool_ids_attribute():
    """Pins the premise: if an Agent.tool_ids ever appears, revisit the fix above."""
    from agents.task.agent.service import Agent

    assert not hasattr(Agent, "tool_ids")


# --- 3. browser-state fallback ------------------------------------------------

def test_browser_state_failure_yields_a_minimal_state_not_none():
    """All three branches of the state setup must produce a BrowserState."""
    from agents.task.agent.core import step

    src = inspect.getsource(step)
    idx = src.find("Error getting browser state")
    assert idx > 0, "browser-state handler not found — test needs updating"
    block = src[idx:idx + 900]
    assert "state = None" not in block, (
        "the browser-state handler still assigns None; downstream "
        "AgentMessagePrompt.get_user_message dereferences state.screenshot "
        "unconditionally, so this crashes run() instead of degrading"
    )
    assert "BrowserState(" in block


def test_prompt_builder_still_dereferences_state_unconditionally():
    """Pins the premise for the fix above: get_user_message reads state.screenshot
    regardless of include_browser_state, so a None state is fatal."""
    from agents.task.agent import prompts

    src = inspect.getsource(prompts.AgentMessagePrompt.get_user_message)
    assert "self.state.screenshot" in src
