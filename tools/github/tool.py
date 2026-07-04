"""``github`` tool (P0-E): PRs/issues/actions over the GitHub REST API.

Auth = a bearer token from ``GITHUB_TOKEN``/``GH_TOKEN`` (env) — the working path today.
An OAuthManager per-(user_id, "github") Fernet store is an OPTIONAL injection seam
(``self._oauth_manager``, default None ⇒ NOT wired), left for a future multi-user setup. The token stays IN-PROCESS only — ``GitHubClient`` calls the REST API
directly over httpx (no subprocess is ever spawned, so the ``build_child_env``/
``SECRET_PAT`` sandbox env-scrub used by ``code_exec`` doesn't apply here and isn't
needed) — and it is never logged or returned in an ``ActionResult``. The GitHubClient
is injectable (``self._client_factory``) so tests never hit the network. Mutating
actions (open_pr/merge_pr/pr_comment/issue_create) are high-impact (approval-gated +
leaf-blocked at the gate layer, Task 9).

LANDMINE: NO ``from __future__ import annotations``.
"""
import logging
import os
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool

# owner/name only: exactly one '/', no traversal, no fragment/query metacharacters,
# no whitespace. httpx normalizes ".." path segments and treats "#"/"?" as
# fragment/query delimiters, so an unvalidated repo spliced into f"/repos/{repo}/..."
# can redirect a mutating call to an unintended repo within the token's scope
# (P0-8 review). We REFUSE anything that isn't exactly one owner/name pair rather
# than sanitizing it, so two distinct inputs can never quietly collapse onto the
# same request path.
_REPO_SLUG_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def _validate_repo(repo: str) -> Optional[str]:
    """Return an error message if ``repo`` isn't a safe single 'owner/name' slug, else None."""
    if not isinstance(repo, str) or ".." in repo or not _REPO_SLUG_RE.fullmatch(repo):
        return f"refused: invalid repo '{repo}' (expected owner/name)"
    return None


class OpenPRParams(BaseModel):
    repo: str = Field(..., description="owner/name")
    title: str = Field(..., description="PR title")
    head: str = Field(..., description="Source branch")
    base: str = Field("main", description="Target branch")
    body: str = Field("", description="PR description")


class PRViewParams(BaseModel):
    repo: str = Field(..., description="owner/name")
    number: int = Field(..., description="PR number")


class PRCommentParams(BaseModel):
    repo: str = Field(..., description="owner/name")
    number: int = Field(..., description="PR/issue number")
    body: str = Field(..., description="Comment body")


class IssueCreateParams(BaseModel):
    repo: str = Field(..., description="owner/name")
    title: str = Field(..., description="Issue title")
    body: str = Field("", description="Issue body")


class IssueListParams(BaseModel):
    repo: str = Field(..., description="owner/name")
    state: Literal["open", "closed", "all"] = Field("open", description="open|closed|all")


class ActionsRunsParams(BaseModel):
    repo: str = Field(..., description="owner/name")


class ActionsLogsParams(BaseModel):
    repo: str = Field(..., description="owner/name")
    run_id: int = Field(..., description="Workflow run id")


class MergePRParams(BaseModel):
    repo: str = Field(..., description="owner/name")
    number: int = Field(..., description="PR number")
    method: Literal["merge", "squash", "rebase"] = Field("merge", description="merge|squash|rebase")


def _short(obj) -> str:
    import json
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    return s if len(s) <= 2000 else s[:2000] + " …[truncated]"


class GitHubTool(BaseTool):
    """GitHub PR/issue/actions surface. Gated by GITHUB_TOOL_ENABLED."""

    def __init__(self, name: str = "github", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        # Injectable for tests: callable(token:str) -> client-like object.
        self._client_factory = None
        # Optional OAuthManager injection seam; None ⇒ env-token auth (GITHUB_TOKEN/GH_TOKEN).
        # Not wired by default (single-user coding agent); a future multi-user setup can inject one.
        self._oauth_manager = None

    @staticmethod
    def _ok(content):
        from tools.controller.types import ActionResult
        return ActionResult(extracted_content=content)

    @staticmethod
    def _err(msg):
        from tools.controller.types import ActionResult
        return ActionResult(error=msg)

    # -- auth -----------------------------------------------------------------

    async def _resolve_token(self, execution_context=None) -> Optional[str]:
        user_id = getattr(execution_context, "user_id", None) or "local"
        mgr = self._oauth_manager
        if mgr is not None:
            try:
                tok = await mgr.get_token(user_id, "github")
                if tok and tok.access_token:
                    return tok.access_token
            except Exception as e:
                getattr(self, "logger", logging.getLogger(__name__)).debug(f"github oauth token miss: {e}")
        return os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")

    async def _client(self, execution_context=None):
        token = await self._resolve_token(execution_context)
        if not token:
            return None, ("no GitHub token: set GITHUB_TOKEN/GH_TOKEN or store an OAuth "
                          "token for provider 'github'")
        if self._client_factory is not None:
            return self._client_factory(token), None
        from tools.github.client import GitHubClient
        return GitHubClient(token), None

    async def _do(self, execution_context, fn):
        client, err = await self._client(execution_context)
        if err:
            return self._err(err)
        try:
            import asyncio
            result = await asyncio.to_thread(fn, client)
            return self._ok(result if isinstance(result, str) else _short(result))
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"github call failed: {e}")
            return self._err(_short(f"github call failed: {e}"))

    # -- actions --------------------------------------------------------------

    @BaseTool.action("Open a pull request — high-impact, approval-gated", param_model=OpenPRParams)
    async def github_open_pr(self, params: OpenPRParams, execution_context=None):
        err = _validate_repo(params.repo)
        if err:
            return self._err(err)
        def _fn(c):
            pr = c.open_pull_request(params.repo, params.title, params.head, params.base, params.body)
            return f"Opened PR #{pr.get('number')}: {pr.get('html_url')}"
        return await self._do(execution_context, _fn)

    @BaseTool.action("View a pull request", param_model=PRViewParams)
    async def github_pr_view(self, params: PRViewParams, execution_context=None):
        err = _validate_repo(params.repo)
        if err:
            return self._err(err)
        def _fn(c):
            pr = c.get_pull_request(params.repo, params.number)
            return f"PR #{pr.get('number')} [{pr.get('state')}] {pr.get('title')}\n{pr.get('html_url')}"
        return await self._do(execution_context, _fn)

    @BaseTool.action("Comment on a PR/issue — high-impact, approval-gated", param_model=PRCommentParams)
    async def github_pr_comment(self, params: PRCommentParams, execution_context=None):
        err = _validate_repo(params.repo)
        if err:
            return self._err(err)
        def _fn(c):
            r = c.comment_issue(params.repo, params.number, params.body)
            return f"Commented: {r.get('html_url')}"
        return await self._do(execution_context, _fn)

    @BaseTool.action("Create an issue — high-impact, approval-gated", param_model=IssueCreateParams)
    async def github_issue_create(self, params: IssueCreateParams, execution_context=None):
        err = _validate_repo(params.repo)
        if err:
            return self._err(err)
        def _fn(c):
            i = c.create_issue(params.repo, params.title, params.body)
            return f"Opened issue #{i.get('number')}: {i.get('html_url')}"
        return await self._do(execution_context, _fn)

    @BaseTool.action("List issues", param_model=IssueListParams)
    async def github_issue_list(self, params: IssueListParams, execution_context=None):
        err = _validate_repo(params.repo)
        if err:
            return self._err(err)
        def _fn(c):
            items = c.list_issues(params.repo, params.state)
            lines = [f"#{it.get('number')} {it.get('title')}" for it in (items or []) if isinstance(it, dict)]
            return "\n".join(lines) or "(no issues)"
        return await self._do(execution_context, _fn)

    @BaseTool.action("List recent GitHub Actions runs", param_model=ActionsRunsParams)
    async def github_actions_runs(self, params: ActionsRunsParams, execution_context=None):
        err = _validate_repo(params.repo)
        if err:
            return self._err(err)
        def _fn(c):
            data = c.list_workflow_runs(params.repo)
            runs = (data or {}).get("workflow_runs", []) if isinstance(data, dict) else []
            lines = [f"{r.get('id')} {r.get('name')} [{r.get('status')}/{r.get('conclusion')}]" for r in runs]
            return "\n".join(lines) or "(no runs)"
        return await self._do(execution_context, _fn)

    @BaseTool.action("Get a workflow run's status/logs pointer", param_model=ActionsLogsParams)
    async def github_actions_logs(self, params: ActionsLogsParams, execution_context=None):
        err = _validate_repo(params.repo)
        if err:
            return self._err(err)
        def _fn(c):
            r = c.get_workflow_run_logs_url(params.repo, params.run_id)
            if isinstance(r, dict):
                return f"run {r.get('id')} [{r.get('status')}/{r.get('conclusion')}] logs: {r.get('logs_url')}"
            return str(r)
        return await self._do(execution_context, _fn)

    @BaseTool.action("Merge a pull request — high-impact, approval-gated", param_model=MergePRParams)
    async def github_merge_pr(self, params: MergePRParams, execution_context=None):
        err = _validate_repo(params.repo)
        if err:
            return self._err(err)
        def _fn(c):
            r = c.merge_pull_request(params.repo, params.number, params.method)
            return f"Merged PR #{params.number}: {r.get('merged')} {r.get('message', '')}"
        return await self._do(execution_context, _fn)
