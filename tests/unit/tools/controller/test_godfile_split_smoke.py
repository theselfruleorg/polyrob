"""UP-11: god-file split smoke — Controller's public surface must be invariant.

A pure code-motion is "correct" iff Controller still exposes every load-bearing name
and the bare-construction idiom + lazy delegator properties still work. This asserts
*where* code lives changed, not *what* Controller exposes.
"""
import logging

import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
from tools.controller.service import Controller, ToolInfo, make_denylist_hook, build_load_skill_result


PUBLIC = [
    # execution
    "act", "multi_act", "_get_operation_key", "_get_retry_limit_for_tool", "_capture_tool_telemetry",
    # hooks (delegators)
    "register_pre_tool_call_hook", "register_post_tool_call_hook", "register_transform_tool_result_hook",
    "_run_pre_tool_call_hooks", "_run_post_tool_call_hooks", "_run_transform_tool_result_hooks",
    "_pre_tool_call_hooks", "_post_tool_call_hooks", "_transform_tool_result_hooks",
    # registration shims (names preserved) — incl. UP-09's memory tool action (UP-11 blocking fix)
    "_register_default_actions", "_register_session_search_action", "_register_memory_tool_action",
    "_register_subtask_action", "_register_backward_compat_aliases",
    # mcp shims
    "_register_mcp_tools_as_direct_actions", "_create_param_model_from_schema", "_mcp_registrar",
    # tool mgmt
    "add_tool", "remove_tool", "get_tool", "has_tool", "list_tools", "load_tools_from_container",
    "_configure_tool",
    # introspection
    "list_actions", "get_action", "action", "get_action_schema", "create_action_model",
    "get_all_actions_for_provider", "supports_native_tools", "get_prompt_description",
    "get_mcp_servers_info", "get_polymarket_info", "get_action_names", "has_action",
    "get_action_details", "tool_calls_to_actions", "get_last_validation_errors",
]


def test_public_surface_intact():
    missing = [n for n in PUBLIC if not hasattr(Controller, n)]
    assert not missing, f"Controller lost attributes after split: {missing}"


def test_module_level_helpers_importable():
    assert ToolInfo is not None
    assert callable(make_denylist_hook)
    assert callable(build_load_skill_result)


def test_bare_construction_and_lazy_properties():
    c = object.__new__(Controller)
    c.logger = logging.getLogger("smoke")
    # lazy delegator properties survive object.__new__ (no __init__)
    assert c._mcp_registrar is not None
