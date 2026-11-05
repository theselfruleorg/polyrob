"""102 3.5: a session that left memory leaves no row in the process-wide maps, and a
resume reads each one back. The integration ratchet proves the rows go; this proves
what comes back."""
import asyncio
from types import SimpleNamespace

from agents.task import model_pin_state as pins
from agents.task import session_residency as R
from agents.task.agent.core.run_loop import _track_execution_task
from agents.task.telemetry.sequence import SequenceGenerator

SESSION = "11111111-1111-4111-8111-111111111111"


def test_a_released_todo_list_reloads_from_its_file(tmp_path, monkeypatch):
    from agents.task.path import reset_path_manager
    from tools.task_tool import TaskTool, TodoItem
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    reset_path_manager()
    tool = object.__new__(TaskTool)
    tool._todos, tool._next_id = {}, {}
    tool.logger = SimpleNamespace(debug=print, info=print, warning=print, error=print)
    tool._todos[SESSION] = [TodoItem(id=1, text="write the test")]
    tool._next_id[SESSION] = 2
    tool._save_to_file(SESSION, "u1")

    tool.release_session(SESSION)
    assert SESSION not in tool._todos and SESSION not in tool._next_id

    assert [t.text for t in tool._get_todos(SESSION, "u1")] == ["write the test"]
    assert tool._next_id[SESSION] == 2
    reset_path_manager()


def test_a_reset_sequence_reseeds_from_the_feed(tmp_path):
    (tmp_path / "000041_step.json").write_text("{}")
    SequenceGenerator.reset(SESSION)
    try:
        assert SequenceGenerator.get(SESSION, feed_dir=tmp_path).next() == 42
        SequenceGenerator.reset(SESSION)
        assert SESSION not in SequenceGenerator._instances
        assert SequenceGenerator.get(SESSION, feed_dir=tmp_path).next() == 42, "never writes over a file"
    finally:
        SequenceGenerator.reset(SESSION)


def test_a_forgotten_pin_comes_back_from_the_session_record():
    pins.set_session_reasoning(SESSION, "high")
    pins.forget_session(SESSION)
    assert pins.session_reasoning(SESSION) is None
    pins.remember_session_pin(SESSION, {"model_pin": {"reasoning": "high"}})
    assert pins.session_reasoning(SESSION) == "high"
    pins.forget_session(SESSION)


def test_residency_keeps_no_row_after_eviction():
    registry = SimpleNamespace(rows={"s1": object()}, get=lambda sid: registry.rows.get(sid))
    agent = SimpleNamespace(_registry=registry)
    assert R.residency(agent, "s1") == "resident"
    assert R.set_residency(agent, "s1", "evicting")
    del registry.rows["s1"]
    assert R.residency(agent, "s1") == "evicting"
    assert R.set_residency(agent, "s1", "evicted")
    assert agent._residency == {}
    assert R.residency(agent, "s1") == "evicted"
    assert R.set_residency(agent, "s1", "resident")
    registry.rows["s1"] = object()
    assert R.residency(agent, "s1") == "resident"


def test_a_finished_task_leaves_the_execution_list():
    async def _run():
        done = asyncio.ensure_future(asyncio.sleep(0))
        await done
        live = asyncio.ensure_future(asyncio.sleep(10))
        tasks = [done]
        _track_execution_task(tasks, live)
        assert tasks == [live]
        live.cancel()
    asyncio.run(_run())


def test_a_finished_child_forgets_its_browser_config():
    from agents.task.agent.sub_agent_manager import SubAgentManager
    forgotten = []
    mgr = object.__new__(SubAgentManager)
    mgr.logger = SimpleNamespace(debug=print)
    mgr.orchestrator = SimpleNamespace(browser_manager=SimpleNamespace(
        forget_session_config=forgotten.append))
    mgr._forget_browser_config("child_s1")
    assert forgotten == ["child_s1"]
    mgr.orchestrator = SimpleNamespace()   # no browser in this session
    mgr._forget_browser_config("child_s1")
    assert forgotten == ["child_s1"]


def test_a_session_that_does_not_exist_leaves_no_lock_row():
    from agents.task_agent_lite import TaskAgent
    a = object.__new__(TaskAgent)
    a._initialized, a.task_available = True, True
    a._session_execution_locks = {}
    a.session_manager = SimpleNamespace(get_session_info=lambda sid: None)
    assert asyncio.run(a.run_session("u1", "no-such-id")) == "Session not found or unauthorized"
    assert a._session_execution_locks == {}

    a.session_manager = SimpleNamespace(get_session_info=lambda sid: {"user_id": "someone-else"})
    assert asyncio.run(a.run_session("u1", "not-yours")) == "Session not found or unauthorized"
    assert a._session_execution_locks == {}
