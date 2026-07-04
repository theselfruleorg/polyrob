"""``github`` tool package (P0-E). Gated OFF by default (GITHUB_TOOL_ENABLED), even
locally — GitHub writes are opt-in and separately approval-gated (Task 9).

LANDMINE: the action-closure module (``tool.py``) must NOT use
``from __future__ import annotations``.
"""
from core.env import bool_env as _bool_env


def github_enabled() -> bool:
    return _bool_env("GITHUB_TOOL_ENABLED", False)


def register_github_tool(force: bool = False) -> bool:
    from tools.descriptors import ToolDescriptor, ToolCategory, register_optional_tool
    from tools.github.tool import GitHubTool

    return register_optional_tool(
        "github",
        GitHubTool,
        ToolDescriptor(
            name="github",
            description="GitHub PRs/issues/actions (open_pr/pr_view/pr_comment/issue_create/issue_list/actions_runs/actions_logs/merge_pr)",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=82,
        ),
        github_enabled,
        force=force,
    )


__all__ = ["github_enabled", "register_github_tool"]
