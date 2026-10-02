"""Coding-agent review B6 (2026-09-24) + codex follow-up (2026-09-25): a
public git/GitHub verb's description must state the approval it REALLY gets.
``recommended`` applies only at compute posture >= 2 or by operator choice
(and under full autonomy the provider is auto_notify: proceed + notify); no
lane means the owner is never asked. "approval-gated" promised more."""
import pytest

from core.verb_policy import policy_for
from tools.git.tool import GitTool
from tools.github.tool import GitHubTool


@pytest.mark.parametrize("cls,name", [
    (GitTool, "git_push"), (GitHubTool, "github_open_pr"), (GitHubTool, "github_merge_pr"),
    (GitHubTool, "github_pr_comment"), (GitHubTool, "github_issue_create"),
])
def test_description_matches_the_approval_lane(cls, name):
    row = policy_for(name)
    desc = getattr(cls, name)._description
    assert row is not None
    assert "approval-gated" not in desc
    if set(row.approval) == {"recommended"}:
        assert "posture >= 2" in desc and "full autonomy" in desc
    elif not row.approval:
        assert "does not ask the owner" in desc
