"""031 T7: self-wake / correspondent delivery / conversation resume consult the record."""
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


def _agent(home):
    from agents.task_agent_lite import TaskAgent
    agent = TaskAgent.__new__(TaskAgent)
    agent.config = type("C", (), {"data_dir": str(home)})()
    return agent


def _capture_events(monkeypatch):
    outcomes = []

    class _Log:
        def record(self, kind, **kw):
            outcomes.append((kind, kw.get("outcome"), kw.get("reason")))

    monkeypatch.setattr("core.event_log.event_log_enabled", lambda: True)
    monkeypatch.setattr("core.event_log.get_event_log", lambda *a, **k: _Log())
    return outcomes


@pytest.mark.asyncio
async def test_self_wake_is_dropped_with_reason_while_paused(home, monkeypatch):
    monkeypatch.setenv("SELF_WAKE_ENABLED", "true")
    from core import autonomy_control as ac
    ac.pause(str(home), via="test")
    agent = _agent(home)
    outcomes = _capture_events(monkeypatch)
    assert await agent.deliver_self_wake("sid", "rob", "text", metadata={"source": "goal"}) is False
    assert ("self_wake", "paused", "paused (all) by owner via test") in outcomes


@pytest.mark.asyncio
async def test_correspondent_delivery_is_held_while_paused(home, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    from core import autonomy_control as ac
    ac.pause(str(home), via="test")
    agent = _agent(home)
    recorded = []

    class Store:
        def record_inbound(self, *a, **k):
            recorded.append((a, k))

    agent.container = type("Ct", (), {"get_service": lambda self, n: Store() if n == "conversation_store" else None})()
    agent.session_manager = type("SM", (), {"get_session_info": lambda self, sid: {"user_id": "rob", "status": "completed"}})()
    called = []

    async def _never(*a, **k):
        called.append(a)

    monkeypatch.setattr(agent, "_resolve_or_recreate", _never, raising=False)
    ok = await agent.deliver_correspondent_data("sid", "x@y", "hi", {"message_id": "m1"}, surface="email")
    assert ok is False and called == []
    assert recorded and recorded[0][0][:3] == ("rob", "email", "x@y")


@pytest.mark.asyncio
async def test_correspondent_data_never_reruns_a_finished_task(home, monkeypatch):
    """Assessment §4.3: a reply into a COMPLETED goal session used to recreate the
    agent with the original goal task and run it again. The recreated turn gets a
    bounded reply task instead."""
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    agent = _agent(home)
    agent.container = None
    info = {"user_id": "rob", "status": "completed",
            "request": {"task": "trade the treasury", "max_steps": 40}}
    updates = []

    class SM:
        def get_session_info(self, sid):
            return info

        def update_session_request(self, sid, req):
            updates.append((sid, dict(req)))
            info["request"] = req

    agent.session_manager = SM()

    class Registry:
        def get(self, sid):
            return None

    agent._registry = Registry()
    seen = {}

    async def _recreate(sid, session_info):
        seen["task"] = session_info["request"]["task"]
        seen["max_steps"] = session_info["request"]["max_steps"]

        class Orch:
            def inject_correspondent_message(self, *a, **k):
                return True

        return Orch()

    monkeypatch.setattr(agent, "_resolve_or_recreate", _recreate, raising=False)
    monkeypatch.setattr("agents.task.task_agent_delivery._spawn_detached", lambda coro: coro.close())

    async def _run(*a, **k):
        return None

    monkeypatch.setattr(agent, "run_session", _run, raising=False)
    ok = await agent.deliver_correspondent_data("sid", "x@y", "hi", {}, surface="email")
    assert ok is True
    assert seen["task"].startswith("[correspondent-reply]") and "trade the treasury" not in seen["task"]
    assert seen["max_steps"] <= 8
    assert updates and updates[0][0] == "sid"


@pytest.mark.asyncio
async def test_conversation_resume_is_held_while_paused(home, monkeypatch):
    monkeypatch.setenv("CONVERSATION_RESUME_ENABLED", "true")
    from core import autonomy_control as ac
    ac.pause(str(home), via="test")
    agent = _agent(home)
    ok = await agent._try_conversation_resume("dead", "x@y", "hi", {}, surface="email", store=None)
    assert ok is False


@pytest.mark.asyncio
async def test_delegation_wake_kick_is_held_for_autonomous_sessions_while_paused(home, monkeypatch):
    from core import autonomy_control as ac
    from agents.task.goals.autonomy_marker import mark_autonomous
    from agents.task_agent_lite import TaskAgent
    agent = TaskAgent.__new__(TaskAgent)
    ran = []

    async def _run(uid, sid):
        ran.append(sid)

    agent.run_session = _run
    agent._registry = type("R", (), {"register": lambda self, sid, orch: None})()
    kicks = {}
    for sid in ("auto-kick", "chat-kick"):
        orch = type("O", (), {"user_id": "rob"})()
        # the registration seam wires the kick onto the orchestrator
        TaskAgent.register_orchestrator(agent, sid, orch)
        kicks[sid] = orch._wake_kick
    mark_autonomous("auto-kick")
    ac.pause(str(home), via="test")
    await kicks["auto-kick"]()
    await kicks["chat-kick"]()
    assert ran == ["chat-kick"]   # the owner's own chat keeps its background results
    ac.resume(str(home))
    await kicks["auto-kick"]()
    assert ran == ["chat-kick", "auto-kick"]
