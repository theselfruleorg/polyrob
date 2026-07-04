"""Regression: filesystem writes must honor execution_context.user_id (tenant).

The 2026-06-20 multi-model live-test reproduced a sub-agent tenant leak: a
delegated sub-agent's files landed under ``data/task/_anonymous_/<virtual_session>/``
instead of the parent tenant. Root cause: the filesystem handlers copy
``execution_context.session_id`` and ``workspace_dir`` onto ``self`` but NOT
``execution_context.user_id``. ``_normalize_path`` then resolves the workspace via
``self.user_id`` (None/stale on the shared tool singleton) →
``pm().get_workspace_dir(virtual_session, None)`` → auto-detect can't find the
unregistered virtual session under the tenant → falls back to ``_anonymous_``.

This drives the real ``write_file`` end-to-end with a stale ``self.user_id`` and an
execution_context carrying the tenant, and asserts the file lands under the tenant.
"""
import asyncio
import logging

import pytest


def _fs_tool_stale_user():
    """FileSystem instance like the shared singleton: user_id unset/stale."""
    from tools.filesystem import FileSystem
    t = object.__new__(FileSystem)
    t.logger = logging.getLogger("fs-subagent")
    t.name = "filesystem"
    t._enabled = True
    t.session_id = "old-session"
    t.user_id = None  # <-- shared singleton has no per-call user_id
    t.workspace_dir = None
    t._current_session_id = None

    async def _noop_init():
        return None

    async def _noop_verify(*a, **k):
        return {"verified": True}

    t.ensure_initialized = _noop_init
    t._verify_file_write = _noop_verify
    return t


@pytest.fixture
def _pm(tmp_path):
    from agents.task.path import PathManager, set_path_manager
    pm = PathManager(data_root=str(tmp_path / "data"))
    set_path_manager(pm)
    return pm


def test_write_file_routes_to_execution_context_tenant(_pm):
    from tools.controller.execution_context import ActionExecutionContext
    from tools.controller.views import WriteFileAction

    tool = _fs_tool_stale_user()
    virtual_session = "sub-1-parent123_parent123-abc"
    ctx = ActionExecutionContext(
        session_id=virtual_session,
        user_id="tenantX",
        workspace_dir=None,
        is_sub_agent=True,
    )

    asyncio.run(tool.write_file(
        WriteFileAction(file_path="subtopic_a.md", content="- one\n- two\n- three\n"),
        execution_context=ctx,
    ))

    tenant_ws = _pm.get_workspace_dir(virtual_session, "tenantX")
    anon_ws = _pm.get_workspace_dir(virtual_session, "_anonymous_")
    assert (tenant_ws / "subtopic_a.md").exists(), (
        f"file did not land under tenant; tenant_ws={tenant_ws}")
    assert not (anon_ws / "subtopic_a.md").exists(), (
        "file leaked to _anonymous_ tenant")
