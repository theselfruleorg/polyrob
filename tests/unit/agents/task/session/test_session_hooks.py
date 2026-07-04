"""C-N1 — session + subagent lifecycle hooks.

Mirrors the controller's pre/post/transform tool-call hook pattern at the session
level: on_session_start/end fired by the orchestrator, on_subagent_start/end fired
on the delegation path. All fail-open — a raising hook never breaks the lifecycle.
Hooks may be sync or async.
"""
import logging

import pytest

from agents.task.session.hooks import SessionHooksMixin


class _Host(SessionHooksMixin):
    def __init__(self):
        self.logger = logging.getLogger("test.hooks")


@pytest.mark.asyncio
async def test_session_start_hook_receives_event():
    host = _Host()
    seen = []
    host.register_session_start_hook(lambda event: seen.append(event))
    await host._run_session_start_hooks(session_id="s1", user_id="u1")
    assert seen == [{"session_id": "s1", "user_id": "u1"}]


@pytest.mark.asyncio
async def test_async_hook_is_awaited():
    host = _Host()
    seen = []

    async def ahook(event):
        seen.append(event["session_id"])

    host.register_session_end_hook(ahook)
    await host._run_session_end_hooks(session_id="s2")
    assert seen == ["s2"]


@pytest.mark.asyncio
async def test_hook_failure_is_fail_open_and_continues():
    host = _Host()
    order = []

    def boom(event):
        order.append("boom")
        raise RuntimeError("nope")

    host.register_session_start_hook(boom)
    host.register_session_start_hook(lambda e: order.append("after"))
    # Must not raise; the second hook still runs.
    await host._run_session_start_hooks(session_id="s3")
    assert order == ["boom", "after"]


@pytest.mark.asyncio
async def test_subagent_hooks_register_and_fire():
    host = _Host()
    starts, ends = [], []
    host.register_subagent_start_hook(lambda e: starts.append(e["goal"]))
    host.register_subagent_end_hook(lambda e: ends.append(e["ok"]))
    await host._run_subagent_start_hooks(goal="do x", parent_session_id="s1")
    await host._run_subagent_end_hooks(goal="do x", ok=True)
    assert starts == ["do x"]
    assert ends == [True]


@pytest.mark.asyncio
async def test_no_hooks_is_noop():
    host = _Host()
    # Should not raise with nothing registered.
    await host._run_session_start_hooks(session_id="s9")
    await host._run_session_end_hooks(session_id="s9")
    await host._run_subagent_start_hooks(goal="g")
    await host._run_subagent_end_hooks(goal="g", ok=False)
