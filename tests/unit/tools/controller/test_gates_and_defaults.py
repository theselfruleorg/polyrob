"""P0 Task 9 — gate membership + documented approval default."""
from agents.task.agent.core.correspondent_gate import HIGH_IMPACT_TOOLS, is_high_impact
from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS
from tools.controller.approval import default_approval_required_tools


def test_high_impact_contains_new_tools():
    for name in ("git_push", "github_open_pr", "github_merge_pr", "github_pr_comment",
                 "github_issue_create", "mcp_install", "tool_manage", "self_modify",
                 "git", "github", "process"):
        assert name in HIGH_IMPACT_TOOLS, name
        assert is_high_impact(name), name


def test_delegate_blocked_contains_new_container_tools():
    for name in ("git", "github", "process", "tool_manage", "mcp"):
        assert name in DELEGATE_BLOCKED_TOOLS, name
    # pre-existing entries preserved
    assert {"code_execution", "coding"} <= DELEGATE_BLOCKED_TOOLS


def test_default_approval_required_tools():
    s = set(default_approval_required_tools())
    assert {"git_push", "github_open_pr", "github_merge_pr",
            "mcp_install", "tool_manage", "self_modify"} <= s
