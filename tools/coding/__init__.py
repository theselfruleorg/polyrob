"""Coding tool package (H10-B / H9 SPEC §4).

A minimal, single-user coding tool surface on top of ``tools/code_exec``:
``str_replace`` (exact, unique-or-fail editor), ``grep`` (gitignore-aware search),
``apply_patch`` (unified-diff, reject-on-context-mismatch), and ``run_tests``
(routed through the code_exec backend). Gated OFF by default
(``CODING_TOOLS_ENABLED``) and never in the global default ``tool_ids``.

LANDMINE: the action-closure module (``tool.py``) must NOT use
``from __future__ import annotations``.
"""
from core.env import bool_env as _bool_env


def coding_tools_enabled() -> bool:
    # Explicit env value always wins; otherwise default ON under POLYROB_LOCAL
    # (CODING_TOOLS_ENABLED is in core.config_policy._SAFE_LOCAL_FLAGS).
    import os
    raw = os.getenv("CODING_TOOLS_ENABLED")
    if raw is not None:
        return _bool_env("CODING_TOOLS_ENABLED", False)
    from core.config_policy import _safe_autonomy_default
    return _safe_autonomy_default("CODING_TOOLS_ENABLED")


def register_coding_tool(force: bool = False) -> bool:
    """Register the ``coding`` descriptor + class IFF enabled (or forced).

    Mirrors ``register_code_exec_tool``: no-op (returns False) when the flag is
    off so ``get_tool_class('coding')`` is None on the default path.
    """
    from tools.descriptors import ToolDescriptor, ToolCategory, register_optional_tool
    from tools.coding.tool import CodingTool

    return register_optional_tool(
        "coding",
        CodingTool,
        ToolDescriptor(
            name="coding",
            description="Edit code (str_replace/apply_patch), search (grep), and run tests in the workspace",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        ),
        coding_tools_enabled,
        force=force,
    )


__all__ = ["coding_tools_enabled", "register_coding_tool"]
