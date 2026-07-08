"""Shared pytest fixtures for the ROB test suite."""
import asyncio
import os

import pytest


@pytest.fixture(autouse=True)
def _cancel_leaked_agent_tasks():
    """Cancel TaskAgent's fire-and-forget periodic loops after each test.

    TaskAgent.initialize() spawns ``_periodic_cleanup``/``_periodic_workspace_cleanup``
    via ``asyncio.create_task`` with no owner. When a test's event loop tears down they
    are GC'd while pending → "Task was destroyed but it is pending" + "I/O operation on
    closed file" stderr spam. We cancel them by coroutine name on teardown. Fail-open.
    """
    yield
    try:
        loop = asyncio.get_event_loop()
    except Exception:
        return
    if loop.is_closed():
        return
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    except RuntimeError:
        return
    cancelled = []
    for t in pending:
        coro = t.get_coro()
        name = getattr(coro, "__qualname__", "") or getattr(coro, "__name__", "")
        if "_periodic_cleanup" in name or "_periodic_workspace_cleanup" in name:
            t.cancel()
            cancelled.append(t)
    # Cancellation only takes effect when the loop steps again; drain here so the
    # tasks are actually finished (not merely flagged) before the loop closes.
    if cancelled and not loop.is_running():
        try:
            loop.run_until_complete(asyncio.gather(*cancelled, return_exceptions=True))
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _isolate_path_manager():
    """Reset process-global path state around every test (E1 leak guard).

    set_path_manager (used by build_cli_container and some integration fixtures)
    mutates a module-global singleton with no automatic teardown, so without this
    a project-scoped pm() set in one test would leak into later tests. We also drop
    POLYROB_WORKSPACE_LOCK_DIR (set via os.environ.setdefault in build_cli_container) and
    reset the interactive busy-depth, so the cross-process workspace lock doesn't
    leak a stale lock dir into an unrelated test. Reset before AND after so each test
    starts from — and leaves — the lazy default. Fail-open.
    """
    os.environ.pop("POLYROB_WORKSPACE_LOCK_DIR", None)
    # build_cli_container also does os.environ.setdefault("POLYROB_LOCAL", "1")
    # (bootstrap.py), which persists process-wide and flips the SAFE autonomy
    # defaults (e.g. CODING_TOOLS_ENABLED) ON — leaking into unrelated tests.
    os.environ.pop("POLYROB_LOCAL", None)
    # The `--project` CLI callback (cli/polyrob.py) does a RAW os.environ set of
    # POLYROB_PROJECT_DIR; a test that exercises it with monkeypatch.delenv(...,
    # raising=False) on an ABSENT key registers no teardown, so the value leaks
    # process-wide and poisons data-home/path-injection tests that read these vars.
    # Clear both project/data-home vars around every test. Fail-open.
    os.environ.pop("POLYROB_PROJECT_DIR", None)
    os.environ.pop("POLYROB_DATA_DIR", None)
    try:
        from agents.task.path import reset_path_manager
        reset_path_manager()
    except Exception:
        reset_path_manager = None  # type: ignore
    try:
        import core.interactive_gate as _ig
        _ig._busy_depth = 0
    except Exception:
        pass
    try:
        yield
    finally:
        os.environ.pop("POLYROB_WORKSPACE_LOCK_DIR", None)
        os.environ.pop("POLYROB_LOCAL", None)
        os.environ.pop("POLYROB_PROJECT_DIR", None)
        os.environ.pop("POLYROB_DATA_DIR", None)
        if reset_path_manager is not None:
            reset_path_manager()


@pytest.fixture(autouse=True)
def _isolate_autonomy_state_store():
    """Keep restart-durable autonomy state OUT of the developer's real data home.

    ``get_autonomy_state_store()`` resolves ``autonomy_state.db`` under
    ``get_data_root()`` — on a dev machine that is the repo's ``.polyrob``. Any test
    touching the ReentryBudget singleton or an orchestrator would persist budget/
    delegation rows there and leak them into later test runs (this bit
    test_self_wake's singleton test on its second run). Durability tests inject an
    explicit store/tmp path, so forcing the flag off here doesn't reduce coverage.
    A test may still opt in by setting AUTONOMY_STATE_DURABLE inside its own body.
    """
    prev = os.environ.get("AUTONOMY_STATE_DURABLE")
    os.environ["AUTONOMY_STATE_DURABLE"] = "off"
    try:
        from agents.task.agent.core.self_wake import reset_reentry_budget
        reset_reentry_budget()
    except Exception:
        pass
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("AUTONOMY_STATE_DURABLE", None)
        else:
            os.environ["AUTONOMY_STATE_DURABLE"] = prev


@pytest.fixture(autouse=True)
def _isolate_external_skill_roots(monkeypatch):
    """Default external (agentskills.io ecosystem) skill discovery to zero roots for
    every test (Task 14, ``skill_discovery.user_external_roots``).

    That function reads real host paths (``~/.agents/skills``, ``~/.claude/skills``)
    by default. On a dev machine where either is populated — e.g. a developer using
    Claude Code has plugin skills under ``~/.claude/skills`` — a plain
    ``SkillManager()`` would silently pick up host-dependent catalog entries,
    breaking hermetic test isolation (this bit two pre-existing catalog tests before
    this guard was added). A test that wants to exercise the real merge overrides
    this explicitly via ``monkeypatch.setattr`` in its own body (see
    ``test_skill_discovery_user_scope.py``), which wins over this default because it
    runs later, inside the test. Fail-open.
    """
    try:
        from agents.task.agent import skill_discovery
        monkeypatch.setattr(skill_discovery, "user_external_roots", lambda: [], raising=False)
    except Exception:
        pass
