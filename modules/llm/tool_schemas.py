"""Reads of a provider tool schema list."""
from typing import Any, List, Optional, Sequence


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
