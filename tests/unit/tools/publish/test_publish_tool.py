"""The agent-facing ship rail: build a page, get a URL, after one owner decision.

Every "ship" goal last week ended as a request to the owner because the agent had
no publish verb at all. This gives it one, with the same shape hf_deploy uses: a
FIRST publish of a new public address needs an approving provider; an already
approved slug iterates unattended.

The hard lines: a leaf/sub-agent or a forged (self-wake / delegation-result) turn
can never publish, a slug is validated before it becomes a path or a URL, and a
publish can only copy files out of the session workspace.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.publish import PublishStore
from tools.publish.tool import PublishTool, PublishParams, UnpublishParams


class _AlwaysApprove:
    async def request(self, action_name, params, context=None):
        return True


class _AlwaysDeny:
    async def request(self, action_name, params, context=None):
        return False


def _ctx(user_id="rob", *, leaf=False, forged=False):
    return SimpleNamespace(
        user_id=user_id, session_id="s1", role="leaf" if leaf else "orchestrator",
        is_sub_agent=leaf, workspace_dir=None,
        metadata={"turn_kind": "self_wake"} if forged else {},
    )


@pytest.fixture
def tool(tmp_path, monkeypatch):
    # A real owner principal — the gate is genuinely exercised, not bypassed.
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    store = PublishStore(db_path=str(tmp_path / "pub.db"),
                         root=str(tmp_path / "publish"),
                         base_url="https://pub.example.com")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html>rob status</html>")

    t = object.__new__(PublishTool)
    t._store = store
    t._approval_provider = _AlwaysApprove()
    t._workspace_override = str(workspace)
    t.name = "publish"
    return t


def _run(coro):
    return asyncio.run(coro)


def test_first_publish_of_a_new_slug_returns_a_url(tool):
    res = _run(tool.publish(PublishParams(slug="rob-status", files=["index.html"]),
                            execution_context=_ctx()))

    assert "https://pub.example.com/rob-status/" in str(res.extracted_content)
    assert tool._store.is_live("rob-status") is True


def test_a_denied_first_publish_is_not_served(tool):
    tool._approval_provider = _AlwaysDeny()

    res = _run(tool.publish(PublishParams(slug="rob-status", files=["index.html"]),
                            execution_context=_ctx()))

    assert tool._store.is_live("rob-status") is False
    assert "approval" in str(res.error).lower()


def test_a_second_publish_of_an_approved_slug_skips_approval(tool):
    _run(tool.publish(PublishParams(slug="rob-status", files=["index.html"]),
                      execution_context=_ctx()))
    tool._approval_provider = _AlwaysDeny()   # would refuse a NEW slug

    res = _run(tool.publish(PublishParams(slug="rob-status", files=["index.html"]),
                            execution_context=_ctx()))

    assert tool._store.is_live("rob-status") is True
    assert "https://pub.example.com/rob-status/" in str(res.extracted_content)


def test_a_leaf_subagent_can_never_publish(tool):
    res = _run(tool.publish(PublishParams(slug="rob-status", files=["index.html"]),
                            execution_context=_ctx(leaf=True)))

    assert res.error
    assert tool._store.is_live("rob-status") is False


def test_a_forged_turn_can_never_publish(tool):
    """A self-wake or delegation-result re-entry is not an owner asking to ship."""
    res = _run(tool.publish(PublishParams(slug="rob-status", files=["index.html"]),
                            execution_context=_ctx(forged=True)))

    assert res.error
    assert tool._store.is_live("rob-status") is False


def test_a_hostile_slug_is_refused(tool):
    res = _run(tool.publish(PublishParams(slug="../../etc/nginx", files=["index.html"]),
                            execution_context=_ctx()))

    assert res.error


def test_a_file_outside_the_workspace_is_refused(tool):
    res = _run(tool.publish(PublishParams(slug="leak", files=["/etc/passwd"]),
                            execution_context=_ctx()))

    assert res.error
    assert tool._store.is_live("leak") is False


def test_unpublish_takes_the_page_down(tool):
    _run(tool.publish(PublishParams(slug="rob-status", files=["index.html"]),
                      execution_context=_ctx()))

    _run(tool.unpublish(UnpublishParams(slug="rob-status"), execution_context=_ctx()))

    assert tool._store.is_live("rob-status") is False


def test_publish_list_shows_the_url_and_status(tool):
    _run(tool.publish(PublishParams(slug="rob-status", files=["index.html"]),
                      execution_context=_ctx()))

    out = str(_run(tool.publish_list(SimpleNamespace(), execution_context=_ctx())).extracted_content)

    assert "rob-status" in out
    assert "https://pub.example.com/rob-status/" in out
    assert "live" in out
