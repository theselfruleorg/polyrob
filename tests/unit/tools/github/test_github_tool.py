"""P0 Task 8 — GitHubTool with a fake client (no network)."""
import logging

import pytest
from pydantic import ValidationError

from tools.github import register_github_tool, github_enabled
from tools.github.tool import GitHubTool, OpenPRParams, PRViewParams, MergePRParams, IssueListParams


class _FakeClient:
    def __init__(self, token):
        self.token = token
        self.calls = []

    def open_pull_request(self, repo, title, head, base, body=""):
        self.calls.append(("open", repo))
        return {"number": 7, "html_url": f"https://github.com/{repo}/pull/7"}

    def get_pull_request(self, repo, number):
        return {"number": number, "state": "open", "title": "T", "html_url": "u"}

    def merge_pull_request(self, repo, number, method="merge"):
        return {"merged": True, "message": "merged"}


def _tool(factory=None):
    t = object.__new__(GitHubTool)
    t.logger = logging.getLogger("gh-test")
    t._client_factory = factory
    t._oauth_manager = None
    return t


class _Ctx:
    user_id = "u1"


# --- registration ------------------------------------------------------------

def test_flag_off_not_registered(monkeypatch):
    monkeypatch.delenv("GITHUB_TOOL_ENABLED", raising=False)
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_class
    TOOL_DESCRIPTORS.pop("github", None)
    assert register_github_tool() is False
    assert get_tool_class("github") is None


def test_flag_on_registers(monkeypatch):
    monkeypatch.setenv("GITHUB_TOOL_ENABLED", "true")
    from tools.descriptors import TOOL_DESCRIPTORS, TOOL_COMPONENTS, get_tool_class
    try:
        assert register_github_tool() is True
        assert get_tool_class("github") is GitHubTool
    finally:
        TOOL_DESCRIPTORS.pop("github", None)
        TOOL_COMPONENTS[:] = [(n, c) for (n, c) in TOOL_COMPONENTS if n != "github"]


def test_not_safe_local(monkeypatch):
    monkeypatch.delenv("GITHUB_TOOL_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    assert github_enabled() is False  # github write stays opt-in even locally


def test_no_future_annotations():
    import __future__
    import tools.github.tool as m
    assert getattr(m, "annotations", None) is not __future__.annotations


# --- behavior ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_pr_returns_url(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    seen = {}
    def factory(token):
        seen["token"] = token
        return _FakeClient(token)
    res = await _tool(factory).github_open_pr(
        OpenPRParams(repo="o/r", title="t", head="feat", base="main"), execution_context=_Ctx()
    )
    assert res.error is None and "pull/7" in res.extracted_content
    assert seen["token"] == "ghp_fake"


@pytest.mark.asyncio
async def test_missing_token_clear_error(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    res = await _tool(lambda token: _FakeClient(token)).github_pr_view(
        PRViewParams(repo="o/r", number=1), execution_context=_Ctx()
    )
    assert res.error and "token" in res.error


@pytest.mark.asyncio
async def test_merge_pr(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    res = await _tool(lambda token: _FakeClient(token)).github_merge_pr(
        MergePRParams(repo="o/r", number=7), execution_context=_Ctx()
    )
    assert res.error is None and "True" in res.extracted_content


@pytest.mark.asyncio
async def test_oauth_token_used_over_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    class _Tok:
        access_token = "oauth-tok"

    class _Mgr:
        async def get_token(self, user_id, provider):
            assert provider == "github"
            return _Tok()

    seen = {}
    def factory(token):
        seen["token"] = token
        return _FakeClient(token)
    t = _tool(factory)
    t._oauth_manager = _Mgr()
    res = await t.github_pr_view(PRViewParams(repo="o/r", number=2), execution_context=_Ctx())
    assert res.error is None and seen["token"] == "oauth-tok"


# --- P0-8 review: repo-slug path-injection guard ------------------------------
#
# repo="owner/name" is spliced straight into request paths (e.g. f"/repos/{repo}/pulls").
# httpx normalizes ".." path segments, so an unvalidated repo can redirect a mutating
# call to an unintended repo within the token's scope, and "#"/"?" corrupt the path via
# fragment/query injection. Every action must refuse anything that isn't exactly one
# "owner/name" pair BEFORE the client is ever constructed/called.

def _counting_factory(calls):
    def factory(token):
        calls.append(token)
        return _FakeClient(token)
    return factory


@pytest.mark.asyncio
async def test_open_pr_rejects_path_traversal_repo(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    calls = []
    res = await _tool(_counting_factory(calls)).github_open_pr(
        OpenPRParams(repo="ownerA/repoA/../../ownerB/repoB", title="t", head="feat", base="main"),
        execution_context=_Ctx(),
    )
    assert res.error is not None
    assert "refused" in res.error and "invalid" in res.error
    assert calls == []  # client was never constructed -> no HTTP request was ever possible


@pytest.mark.asyncio
async def test_open_pr_rejects_fragment_in_repo(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    calls = []
    res = await _tool(_counting_factory(calls)).github_open_pr(
        OpenPRParams(repo="o/r#x", title="t", head="feat", base="main"),
        execution_context=_Ctx(),
    )
    assert res.error is not None and "refused" in res.error
    assert calls == []


@pytest.mark.asyncio
async def test_open_pr_rejects_query_in_repo(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    calls = []
    res = await _tool(_counting_factory(calls)).github_open_pr(
        OpenPRParams(repo="o/r?x", title="t", head="feat", base="main"),
        execution_context=_Ctx(),
    )
    assert res.error is not None and "refused" in res.error
    assert calls == []


@pytest.mark.asyncio
async def test_open_pr_rejects_extra_slash_in_repo(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    calls = []
    res = await _tool(_counting_factory(calls)).github_open_pr(
        OpenPRParams(repo="a/b/c", title="t", head="feat", base="main"),
        execution_context=_Ctx(),
    )
    assert res.error is not None and "refused" in res.error
    assert calls == []


@pytest.mark.asyncio
async def test_merge_pr_rejects_path_traversal_repo(monkeypatch):
    # Cross-check: the guard is applied to a SECOND mutating action, not just open_pr.
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    calls = []
    res = await _tool(_counting_factory(calls)).github_merge_pr(
        MergePRParams(repo="o/r/../../evil", number=7), execution_context=_Ctx()
    )
    assert res.error is not None and "refused" in res.error
    assert calls == []


@pytest.mark.asyncio
async def test_pr_view_rejects_path_traversal_repo(monkeypatch):
    # Cross-check: the guard also covers a READ-only action, not just mutating ones.
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    calls = []
    res = await _tool(_counting_factory(calls)).github_pr_view(
        PRViewParams(repo="o/r/../../evil", number=1), execution_context=_Ctx()
    )
    assert res.error is not None and "refused" in res.error
    assert calls == []


@pytest.mark.asyncio
async def test_open_pr_valid_repo_still_works(monkeypatch):
    # Happy path must survive the new guard unchanged.
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    calls = []
    res = await _tool(_counting_factory(calls)).github_open_pr(
        OpenPRParams(repo="owner/name", title="t", head="feat", base="main"),
        execution_context=_Ctx(),
    )
    assert res.error is None and "pull/7" in res.extracted_content
    assert calls == ["ghp_fake"]


# --- P0 review: state/method are typed enums, not free strings -----------------

def test_issue_list_state_rejects_invalid_enum():
    with pytest.raises(ValidationError):
        IssueListParams(repo="o/r", state="bogus")


def test_issue_list_state_accepts_documented_values():
    for v in ("open", "closed", "all"):
        assert IssueListParams(repo="o/r", state=v).state == v


def test_merge_pr_method_rejects_invalid_enum():
    with pytest.raises(ValidationError):
        MergePRParams(repo="o/r", number=1, method="bogus")


def test_merge_pr_method_accepts_documented_values():
    for v in ("merge", "squash", "rebase"):
        assert MergePRParams(repo="o/r", number=1, method=v).method == v


# --- P0 review: except-path error text is capped, like the success path --------

@pytest.mark.asyncio
async def test_call_failure_error_is_capped(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

    class _BoomClient:
        def __init__(self, token):
            pass

        def get_pull_request(self, repo, number):
            raise RuntimeError("x" * 5000)  # simulate a huge/uncapped exception message

    res = await _tool(lambda token: _BoomClient(token)).github_pr_view(
        PRViewParams(repo="o/r", number=1), execution_context=_Ctx()
    )
    assert res.error is not None
    assert "github call failed" in res.error
    assert len(res.error) <= 2000 + len(" …[truncated]")
