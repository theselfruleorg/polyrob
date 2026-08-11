"""Background maintenance must not do blocking SQLite/file I/O on the shared loop.

Both the curator ticker and the periodic H-MEM save ran synchronous SQLite and
filesystem work inline on the event loop that also serves live chat, API, goal and
cron sessions. `core/sqlite_util.execute_retry` does a real `time.sleep` (up to 15
retries x up to 0.15s) on write contention, so a single curator tick landing during a
concurrent memory write could stall EVERY session in the process for seconds.

The memory provider already routes its hot-path methods through `run_in_executor` for
exactly this reason (its `_run_blocking` docstring says running them inline "would
freeze the ENTIRE loop (every concurrent session), not just the calling coroutine") —
these two callers were simply not covered.
"""
import asyncio
import inspect
import threading

import pytest


@pytest.mark.asyncio
async def test_curator_run_once_does_not_run_on_the_loop_thread():
    """The pass body must execute on a worker thread, not the loop thread."""
    from agents.task.agent.core.curator import SkillCurator

    loop_thread = threading.get_ident()
    ran_on = {}

    class _Usage:
        def list_authored(self, created_by=None):
            ran_on["thread"] = threading.get_ident()
            return []

        def get_state(self, k):
            return None

        def set_state(self, k, v):
            pass

    cur = SkillCurator(skill_manager=object(), usage_store=_Usage())
    await cur.run_once()

    assert ran_on.get("thread") is not None, "curator body never ran"
    assert ran_on["thread"] != loop_thread, (
        "curator ran its blocking SQLite pass on the event loop thread — a tick "
        "during write contention stalls every concurrent session"
    )


@pytest.mark.asyncio
async def test_curator_run_once_still_returns_the_pass_result():
    """Offloading must not change the contract the ticker depends on."""
    from agents.task.agent.core.curator import SkillCurator

    class _Usage:
        def list_authored(self, created_by=None):
            return []

        def get_state(self, k):
            return None

        def set_state(self, k, v):
            pass

    cur = SkillCurator(skill_manager=object(), usage_store=_Usage())
    result = await cur.run_once()
    assert isinstance(result, dict)
    assert "transitions" in result


@pytest.mark.asyncio
async def test_curator_run_once_stays_fail_open():
    """A raising store must not propagate out of the ticker."""
    from agents.task.agent.core.curator import SkillCurator

    class _Usage:
        def list_authored(self, created_by=None):
            raise RuntimeError("db exploded")

        def get_state(self, k):
            return None

        def set_state(self, k, v):
            pass

    cur = SkillCurator(skill_manager=object(), usage_store=_Usage())
    result = await cur.run_once()
    assert "error" in result


def test_periodic_hmem_save_is_offloaded():
    """The every-10-steps save_session is a mkdir + open + json.dump of the whole
    session state; it sits three lines after a deliberate to_thread offload of its
    sibling add_step_memory call and must get the same treatment.

    AST rather than text: `save_session` must appear as an ARGUMENT to
    `asyncio.to_thread(...)`, never as the function being called directly.
    """
    import ast

    from agents.task.agent.core import memory_writer

    tree = ast.parse(inspect.getsource(memory_writer))

    offloaded, direct = False, []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # asyncio.to_thread(<something>.save_session, ...)
        if isinstance(fn, ast.Attribute) and fn.attr == "to_thread":
            for arg in node.args:
                if isinstance(arg, ast.Attribute) and arg.attr == "save_session":
                    offloaded = True
        # <something>.save_session(...) called directly
        elif isinstance(fn, ast.Attribute) and fn.attr == "save_session":
            direct.append(node.lineno)

    assert offloaded, "save_session is not passed to asyncio.to_thread"
    assert not direct, (
        f"save_session is called directly at line(s) {direct} — it runs inline on the "
        "event loop, so every active session blocks the whole process for the duration "
        "of its own H-MEM write, every 10 steps"
    )


def test_curator_body_is_separated_from_the_async_entry_point():
    """Structural: guards against a future edit inlining blocking work back into the
    coroutine."""
    from agents.task.agent.core.curator import SkillCurator

    assert inspect.iscoroutinefunction(SkillCurator.run_once)
    assert not inspect.iscoroutinefunction(SkillCurator._run_once_blocking)
    body = inspect.getsource(SkillCurator.run_once)
    assert "asyncio.to_thread" in body
