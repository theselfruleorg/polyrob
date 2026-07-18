"""Regression (P1 finalization): several bare asyncio.create_task(...) sites in
task_agent_lite held no strong reference, so the task could be GC'd mid-run and
silently dropped (a well-known asyncio footgun). _spawn_detached holds a ref until
the task completes, then self-cleans.
"""
import asyncio
import gc

import pytest

from agents.task_agent_lite import _spawn_detached, _DETACHED_TASKS


@pytest.mark.asyncio
async def test_spawn_detached_holds_ref_and_runs_to_completion():
    ran = asyncio.Event()

    async def _work():
        await asyncio.sleep(0)
        ran.set()

    t = _spawn_detached(_work())
    assert t in _DETACHED_TASKS, "must hold a strong reference while in flight"

    # Drop our local handle and force GC — the set must keep it alive.
    del t
    gc.collect()
    await asyncio.wait_for(ran.wait(), timeout=1.0)
    assert ran.is_set()

    # After completion the done-callback removes it from the set.
    await asyncio.sleep(0)
    assert not any(not x.done() for x in _DETACHED_TASKS)
