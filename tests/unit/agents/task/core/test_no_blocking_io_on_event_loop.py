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


# --- 125 S18b (B-H1): goal-board and cron-store calls from async code ----------------
#
# A GoalBoard / CronJobStore method is a synchronous SQLite call (execute_retry can
# sleep up to ~17 s on a contended file). Called inside an ``async def`` it holds every
# session on the loop. The goal and cron loops await it through
# ``core.async_bridge.off_loop`` (``self._board_io`` / ``self._store_io``).

import ast  # noqa: E402
from pathlib import Path  # noqa: E402

_REPO = Path(__file__).resolve().parents[5]
#: Files that hold ZERO direct store calls inside an ``async def``.
_OFF_LOOP_FILES = (
    "agents/task/goals/dispatcher.py",
    "agents/task/goals/goal_run.py",
    "agents/task/goals/escalation.py",
    "cron/scheduler.py",
)
#: Receivers that name a goal board or a cron store.
_RECEIVERS = {"board", "_board", "goal_board", "store", "_store", "cron_store", "job_store"}
#: Every other production file with a direct store call inside an ``async def``:
#: the count may only go DOWN. Lower a row when you move a call off the loop;
#: delete it at zero.
_ON_LOOP_BASELINE = {
    "api/mcp_serve/handlers.py": 2,
    "core/surfaces/room_actions.py": 7,
    "modules/x402/settlement_subscriptions.py": 3,
    "packs/x/polyrob_x/x_browser/escalation.py": 2,
    "surfaces/telegram/approval_ops.py": 3,
    "tools/controller/approval_queue.py": 6,
    "tools/controller/autonomy_control_action.py": 2,
    "tools/controller/message_delete.py": 1,
    "tools/controller/message_send.py": 1,
    "tools/controller/owner_ask_action.py": 4,
    "tools/controller/setting_actions.py": 1,
    "tools/cronjob_tools.py": 8,
    "tools/dapp_browser/tool.py": 2,
    "tools/email_tool.py": 1,
    "tools/goal_tools.py": 15,
    "tools/publish/tool.py": 2,
    "webview/goal_board_routes.py": 13,
    "webview/job_routes.py": 2,
    "webview/pages.py": 4,
}


def _store_methods() -> set:
    from modules.goals.board import GoalBoard
    from cron.jobs import CronJobStore
    names = set()
    for cls in (GoalBoard, CronJobStore):
        names |= {n for n, _ in inspect.getmembers(cls, callable) if not n.startswith("__")}
    return names


def _direct_store_calls(source: str, methods: set) -> list:
    out = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            recv = node.func.value
            last = recv.attr if isinstance(recv, ast.Attribute) else getattr(recv, "id", None)
            if last in _RECEIVERS and node.func.attr in methods:
                out.append((fn.name, node.lineno, f"{last}.{node.func.attr}"))
    return out


def test_store_calls_are_off_the_loop():
    methods = _store_methods()
    bad = {}
    for rel in _OFF_LOOP_FILES:
        hits = _direct_store_calls((_REPO / rel).read_text(), methods)
        if hits:
            bad[rel] = hits
    assert bad == {}, (
        "a goal-board / cron-store call inside an async def runs on the event loop; "
        f"await it through off_loop (self._board_io / self._store_io): {bad}")


def _production_files():
    from tests.support.repo_index import repo_index
    skip = set(_OFF_LOOP_FILES)
    for rel in repo_index().tracked():
        if not rel.endswith(".py") or rel in skip:
            continue
        top = rel.split("/", 1)[0]
        if top in ("tests", "ops", "build", "docs", "scripts", "devtools") or "/tests/" in rel:
            continue
        yield rel


def test_store_calls_on_loop_only_shrink():
    methods = _store_methods()
    counts = {}
    for rel in _production_files():
        path = _REPO / rel
        if not path.is_file():
            continue
        try:
            n = len(_direct_store_calls(path.read_text(), methods))
        except SyntaxError:
            continue
        if n:
            counts[rel] = n
    grew = {f: (n, _ON_LOOP_BASELINE.get(f, 0)) for f, n in counts.items()
            if n > _ON_LOOP_BASELINE.get(f, 0)}
    assert grew == {}, (
        "new goal-board / cron-store calls on the event loop (file: (now, baseline)); "
        f"await them through core.async_bridge.off_loop: {grew}")
    stale = {f: (counts.get(f, 0), n) for f, n in _ON_LOOP_BASELINE.items()
             if counts.get(f, 0) < n}
    assert stale == {}, f"lower these baseline rows (file: (now, baseline)): {stale}"
