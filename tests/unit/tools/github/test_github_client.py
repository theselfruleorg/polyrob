"""P0 Task 8 — GitHubClient with an injected request fn (no network)."""
import pytest

from tools.github.client import GitHubClient, GitHubError


def test_open_pr_builds_request():
    seen = {}
    def fake_request(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers, body=body)
        return 201, {"number": 3, "html_url": "u"}
    pr = GitHubClient("tok", request=fake_request).open_pull_request("o/r", "t", "h", "b", "desc")
    assert pr["number"] == 3
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/repos/o/r/pulls")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["body"]["head"] == "h" and seen["body"]["base"] == "b"


def test_error_status_raises():
    def fake_request(method, url, headers, body):
        return 422, {"message": "Validation Failed"}
    with pytest.raises(GitHubError):
        GitHubClient("tok", request=fake_request).open_pull_request("o/r", "t", "h", "b")
