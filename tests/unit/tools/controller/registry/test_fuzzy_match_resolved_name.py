"""B7 (high) — tool_call_to_action must key the ActionModel by the RESOLVED
(fuzzy-matched) registered name, not the caller's original tool_name.

_get_action_with_fuzzy_match resolved e.g. 'click_element' -> the registered
'browser_click_element' action for validation, but the ActionModel was then
built as {'click_element': args}. ActionModel has no 'click_element' field, so
the key was dropped -> an EMPTY no-op action (validated args, executed nothing).
"""
from pydantic import BaseModel

from tools.controller.registry.service import Registry


class _ClickParams(BaseModel):
    index: int = 0


async def _click(params, execution_context=None):
    from agents.task.agent.views import ActionResult
    return ActionResult(extracted_content=f"clicked {params.index}")


def _registry():
    reg = Registry()
    reg.wrap_function("browser_click_element", _click, "click an element",
                      tool="browser", param_model=_ClickParams)
    return reg


def test_fuzzy_matched_action_keys_the_resolved_name():
    reg = _registry()
    am = reg.tool_call_to_action("click_element", {"index": 3})  # fuzzy -> browser_click_element
    data = am.model_dump(exclude_none=True)
    assert "browser_click_element" in data, f"resolved name missing: {data}"
    assert data["browser_click_element"] == {"index": 3}


def test_exact_match_still_keys_its_own_name():
    reg = _registry()
    am = reg.tool_call_to_action("browser_click_element", {"index": 7})
    data = am.model_dump(exclude_none=True)
    assert data.get("browser_click_element") == {"index": 7}
