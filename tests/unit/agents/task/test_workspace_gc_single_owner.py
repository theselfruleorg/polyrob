"""The destructive workspace GC must have exactly ONE owner per box.

Prod ran it in TWO processes. `polyrob-webview.service` — a read-only
monitoring console — builds its own TaskAgent, and TaskAgent.initialize()
unconditionally spawns `_periodic_workspace_cleanup`. So the console ran a
daily rmtree over the agent's data.

That is how the project-root guard was defeated for 14 days: the guard shipped
on 2026-08-18, but the webview process had been running since 2026-08-05 and
`deploy_prod.sh` never restarts it. A process started before a fix keeps the
old code in memory forever, and the console wiped the agent's project directory
every day at 06:26 UTC:

    Aug 14..19  06:26:27  polyrob-webview[1854063]: Workspace cleanup: removed 181 old workspaces

The guard is the blast-radius fix. This is the ownership fix: a console has no
business garbage-collecting the agent's workspaces at all.
"""
import asyncio

import pytest


class _StubSessionManager:
    def __init__(self):
        self.calls = 0

    def cleanup_old_workspaces(self, max_age_days=7):
        self.calls += 1
        return 0


def _agent(owns_gc):
    from agents.task_agent_lite import TaskAgent

    agent = object.__new__(TaskAgent)
    agent._bg_tasks = []
    agent._owns_workspace_gc = owns_gc
    agent.session_manager = _StubSessionManager()
    return agent


def test_task_agent_owns_the_gc_by_default():
    """The agent process keeps today's behaviour — no silent GC loss."""
    from agents.task_agent_lite import TaskAgent
    import inspect

    sig = inspect.signature(TaskAgent.__init__)
    assert sig.parameters["owns_workspace_gc"].default is True


def test_webview_builds_its_task_agent_without_the_gc():
    """The console must opt OUT explicitly, in source, not by convention."""
    import re
    from pathlib import Path

    src = Path("webview/server.py").read_text()
    match = re.search(r"TaskAgent\((?P<args>[^)]*)\)", src)

    assert match, "webview no longer constructs a TaskAgent — re-check this guard"
    assert "owns_workspace_gc=False" in match.group("args"), (
        "the webview console must not run the destructive workspace GC")


@pytest.mark.asyncio
async def test_gc_loop_returns_immediately_when_not_the_owner():
    agent = _agent(owns_gc=False)

    # Must fall straight through rather than sleeping for a day.
    await asyncio.wait_for(agent._periodic_workspace_cleanup(), timeout=1)

    assert agent.session_manager.calls == 0


@pytest.mark.asyncio
async def test_gc_loop_runs_when_it_is_the_owner(monkeypatch):
    agent = _agent(owns_gc=True)
    slept = []

    async def _fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await agent._periodic_workspace_cleanup()

    assert agent.session_manager.calls == 1
    assert slept[0] == 86400
