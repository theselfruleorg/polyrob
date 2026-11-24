"""Reads of a provider tool schema list."""
from typing import Any, List, Optional, Sequence

from modules.llm.model_types import model_family


def schema_tool_names(tools: Optional[Sequence[Any]]) -> List[str]:
    """The tool names in the order the schema list emits them.

    Handles both shapes: OpenAI's ``{"type": "function", "function": {"name": ...}}`` and
    Anthropic's flat ``{"name": ...}``. An unknown entry is skipped.
    """
    names: List[str] = []
    for entry in tools or []:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function")
        name = (fn.get("name") if isinstance(fn, dict) else None) or entry.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _sorted_properties(node: Any) -> Any:
    if isinstance(node, list):
        return [_sorted_properties(v) for v in node]
    if not isinstance(node, dict):
        return node
    return {k: ({name: _sorted_properties(v[name]) for name in sorted(v)}
                if k == "properties" and isinstance(v, dict) else _sorted_properties(v))
            for k, v in node.items()}


def tools_in_model_order(tools: Any, model: Optional[str]) -> Any:
    """*tools* with the schema properties in the order *model* writes its arguments.

    GLM writes them in alphabetical order (``limit, room``). Some upstreams decode with the
    schema's order fixed, so a property that the schema puts first and GLM writes later can
    never be written: ``room_read`` came without ``room`` and ``skill_manage`` without
    ``old_string``, call after call, until the run ended as a loop. Other families are
    unchanged. A new list is returned: the caller's schemas are shared and cached.
    """
    if not tools or model_family(model) != "glm":
        return tools
    return _sorted_properties(tools)
