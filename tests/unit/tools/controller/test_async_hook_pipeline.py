"""UP-04 — async HookPipeline: loop-not-blocked + sync/async-hook mix.

The pre/transform/post runners are now ``async def`` and ``await`` each hook. A slow
async hook (e.g. an interactive/remote approval provider) must YIELD the event loop so
concurrent tasks (other sessions / sub-agents sharing the Controller) keep progressing,
instead of freezing the loop the way the old ``run_coroutine_sync`` bridge did. Legacy
sync hooks must keep working unchanged through the same pipeline.
"""
import asyncio
import logging

import pytest

from tools.controller.hooks import HookPipeline
from tools.controller.types import ActionResult


def _pipe() -> HookPipeline:
    return HookPipeline(logging.getLogger("async-hook-pipeline-test"))


@pytest.mark.asyncio
async def test_slow_async_pre_yields_loop():
    """A slow async pre-hook must not block the loop: a concurrent task runs DURING it."""
    p = _pipe()
    started = asyncio.Event()

    async def slow_hook(name, params, ctx):
        started.set()
        await asyncio.sleep(0.2)
        return None  # allow

    p.register_pre(slow_hook)

    progressed = []

    async def other():
        await asyncio.sleep(0.05)
        progressed.append(True)

    reason, _ = await asyncio.gather(p.run_pre("x", {}, None), other())
    assert reason is None             # slow hook allowed
    assert progressed == [True]       # the other task ran while run_pre was awaiting


@pytest.mark.asyncio
async def test_sync_and_async_pre_hooks_mix():
    """A plain-def hook and an async hook coexist; order + first-denial-wins preserved."""
    p = _pipe()
    order = []

    def sync_allow(name, params, ctx):
        order.append("sync")
        return None

    async def async_deny(name, params, ctx):
        order.append("async")
        return "denied-by-async"

    p.register_pre(sync_allow)
    p.register_pre(async_deny)
    reason = await p.run_pre("x", {}, None)
    assert reason == "denied-by-async"
    assert order == ["sync", "async"]   # registration order honoured across sync+async


@pytest.mark.asyncio
async def test_async_transform_chains_with_sync():
    """transform chains a sync hook then an async hook in registration order."""
    p = _pipe()
    p.register_transform(lambda n, pa, r, c: ActionResult(extracted_content="a"))

    async def add_b(n, pa, r, c):
        return ActionResult(extracted_content=r.extracted_content + "b")

    p.register_transform(add_b)
    out = await p.run_transform("read", {}, ActionResult(extracted_content="orig"), None)
    assert out.extracted_content == "ab"


@pytest.mark.asyncio
async def test_async_post_observes_and_open_swallows():
    """post awaits an async observer; an open-mode async raise is swallowed."""
    p = _pipe()
    seen = []

    async def observe(n, pa, r, c):
        seen.append(n)

    async def boom(n, pa, r, c):
        raise RuntimeError("async hook bug")

    p.register_post(observe)
    p.register_post(boom)  # open: swallowed
    await p.run_post("t", {}, ActionResult(extracted_content="x"), None)
    assert seen == ["t"]


@pytest.mark.asyncio
async def test_async_pre_closed_failure_denies():
    """An async pre-hook that raises with fail_mode=closed must DENY."""
    p = _pipe()

    async def boom(n, pa, c):
        raise RuntimeError("async hook bug")

    p.register_pre(boom, fail_mode="closed")
    reason = await p.run_pre("write_file", {}, None)
    assert reason and "hook" in reason.lower()
