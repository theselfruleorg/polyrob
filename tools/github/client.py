"""Minimal GitHub REST client (P0-E).

A thin wrapper over the GitHub REST API v3. The HTTP layer is injectable (``request=``)
so tests never hit the network. The bearer token is passed in by the tool (from the
OAuth store or env); the client itself never reads env and never touches the token
store, so it stays trivially fakeable. Holds no action closures — ``from __future__``
is safe.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote as _urlquote

# (method, url, headers, json_body) -> (status_code, parsed_body)
HttpRequest = Callable[[str, str, Dict[str, str], Optional[Dict[str, Any]]], Tuple[int, Any]]


class GitHubError(RuntimeError):
    """Raised on a non-2xx GitHub response."""


def _repo_path(repo: str) -> str:
    """Percent-encode the owner and name segments of ``repo`` independently.

    Defense-in-depth (P0-8 review) behind the ``tools/github/tool.py`` validator:
    even if a caller reaches this client directly with an unvalidated ``repo``,
    splitting on the FIRST '/' only and encoding each half with ``safe=""`` means
    any extra '/' (path-traversal segments, extra owner/repo pairs) inside either
    half is percent-encoded to ``%2F`` rather than left as a literal path
    separator — so it can never be re-split into additional path segments or
    resolved by URL '..' normalization onto a different repo.
    """
    owner, _sep, name = str(repo).partition("/")
    return f"{_urlquote(owner, safe='')}/{_urlquote(name, safe='')}"


class GitHubClient:
    def __init__(self, token: str, *, base_url: str = "https://api.github.com",
                 request: Optional[HttpRequest] = None) -> None:
        self._token = token
        self._base = base_url.rstrip("/")
        self._request = request or self._default_request

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "polyrob-github-tool",
        }

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = path if path.startswith("http") else f"{self._base}{path}"
        status, data = self._request(method, url, self._headers(), body)
        if status < 200 or status >= 300:
            raise GitHubError(f"GitHub {method} {path} -> {status}: {data}")
        return data

    def _default_request(self, method, url, headers, body):
        import httpx
        with httpx.Client(timeout=30.0) as client:
            resp = client.request(method, url, headers=headers, json=body)
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            return resp.status_code, data

    # -- endpoints ------------------------------------------------------------

    def open_pull_request(self, repo: str, title: str, head: str, base: str, body: str = "") -> Dict[str, Any]:
        return self._call("POST", f"/repos/{_repo_path(repo)}/pulls",
                          {"title": title, "head": head, "base": base, "body": body})

    def get_pull_request(self, repo: str, number: int) -> Dict[str, Any]:
        return self._call("GET", f"/repos/{_repo_path(repo)}/pulls/{number}")

    def comment_issue(self, repo: str, number: int, body: str) -> Dict[str, Any]:
        return self._call("POST", f"/repos/{_repo_path(repo)}/issues/{number}/comments", {"body": body})

    def create_issue(self, repo: str, title: str, body: str = "") -> Dict[str, Any]:
        return self._call("POST", f"/repos/{_repo_path(repo)}/issues", {"title": title, "body": body})

    def list_issues(self, repo: str, state: str = "open") -> Any:
        return self._call("GET", f"/repos/{_repo_path(repo)}/issues?state={state}")

    def list_workflow_runs(self, repo: str) -> Any:
        return self._call("GET", f"/repos/{_repo_path(repo)}/actions/runs")

    def get_workflow_run_logs_url(self, repo: str, run_id: int) -> Any:
        return self._call("GET", f"/repos/{_repo_path(repo)}/actions/runs/{run_id}")

    def merge_pull_request(self, repo: str, number: int, method: str = "merge") -> Dict[str, Any]:
        return self._call("PUT", f"/repos/{_repo_path(repo)}/pulls/{number}/merge", {"merge_method": method})
