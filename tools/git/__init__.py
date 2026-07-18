"""``git`` tool package (P0-D).

Structured git over the confined workspace root. Gated OFF by default
(``GIT_TOOLS_ENABLED``) and ON under ``POLYROB_LOCAL`` (it is in
``core.config_policy._SAFE_LOCAL_FLAGS``). Never in the default ``tool_ids``.

LANDMINE: the action-closure module (``tool.py``) must NOT use
``from __future__ import annotations``.
"""
from core.env import bool_env as _bool_env


def git_enabled() -> bool:
    import os
    raw = os.getenv("GIT_TOOLS_ENABLED")
    if raw is not None:
        return _bool_env("GIT_TOOLS_ENABLED", False)
    from core.config_policy import _safe_autonomy_default
    return _safe_autonomy_default("GIT_TOOLS_ENABLED")


def register_git_tool(force: bool = False) -> bool:
    """Register the ``git`` descriptor + class IFF enabled (or forced)."""
    from tools.descriptors import ToolDescriptor, ToolCategory, register_optional_tool
    from tools.git.tool import GitTool

    return register_optional_tool(
        "git",
        GitTool,
        ToolDescriptor(
            name="git",
            description="Structured git over the workspace (status/diff/log/branch/checkout/add/commit/pull/push/clone)",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=81,
        ),
        git_enabled,
        force=force,
    )


__all__ = ["git_enabled", "register_git_tool"]
