"""The eviction ratchet: a session that left memory leaves nothing that grows.

Real container, real ``chat_once``, real ``_evict_session``. Only the LLM decision
(``get_next_action``) is stubbed. Two things are pinned here, and nobody pinned them
before:

- the agent graph dies: after eviction and an explicit ``gc.collect()`` the
  orchestrator, Agent and Controller weakrefs are dead. The collect
  is explicit because the graph holds cycles, and when the process runs its collect
  is not this test's concern;
- no container grows per session. The scan is by discovery, so a NEW per-session map
  anywhere in the first-party modules or on a container service fails here without
  anybody having to list it.

A row in ``KEPT_ON_PURPOSE`` is a map that must grow, with the reason. A row may only
leave ``KNOWN_LEAKS``: the test fails when a listed map stops growing, so the list
cannot go stale.
"""
from __future__ import annotations

import asyncio
import collections
import gc
import logging
import sys
import types
import weakref
from unittest.mock import AsyncMock, patch

import pytest

from agents.task.agent.views import AgentBrain, AgentOutput

FIRST_PARTY = ("agents", "core", "tools", "modules", "polyrob", "utils", "cron", "surfaces")
SIZED = (dict, list, set, collections.deque, weakref.WeakKeyDictionary, weakref.WeakValueDictionary)

WARM_CYCLES = 3
CYCLES = 5

KEPT_ON_PURPOSE = {
    # The session index: `_durable_chat_session` scans these rows to resume a chat.
    "service:session_manager._sessions",
    "singleton:SessionManager._sessions",
    # The owner-seat half of the autonomy marker: an authority boundary, never capped.
    "core.autonomy_marker._OWNER_SEATS",
}

# Each one holds a row per session and has its own hard cap.
BOUNDED = {
    "agents.task.telemetry.run_activity._activities",  # _MAX_SESSIONS
    "core.prompt_manifest._SEEN",  # _SEEN_MAX
}

KNOWN_LEAKS: set = set()


def _temp_env(monkeypatch, tmp_path):
    for k in ("DATA_DIR", "DATA_ROOT", "CHARACTERS_DIR", "KNOWLEDGE_DIR",
              "CACHE_DIR", "DB_PATH", "TELEMETRY_DATA_DIR"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "polyrob_data"))
    monkeypatch.setenv("MEMORY_BACKEND", "none")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")


def _module_sizes() -> dict:
    """The size of every module-level and class-level container we own."""
    sizes = {"logging.root.manager.loggerDict": len(logging.root.manager.loggerDict)}
    for name, mod in list(sys.modules.items()):
        if not isinstance(mod, types.ModuleType) or name.split(".")[0] not in FIRST_PARTY:
            continue
        for attr, value in list(vars(mod).items()):
            if isinstance(value, SIZED):
                sizes[f"{name}.{attr}"] = len(value)
            elif isinstance(value, type) and value.__module__ == name:
                for cattr, cvalue in list(vars(value).items()):
                    if isinstance(cvalue, SIZED):
                        sizes[f"{name}.{value.__name__}.{cattr}"] = len(cvalue)
    return sizes


def _service_sizes(container) -> dict:
    """The size of every container held on a process-wide service instance."""
    sizes = {}
    for service, registration in list(container._services.items()):
        instance = getattr(registration, "instance", None)
        for attr, value in list(getattr(instance, "__dict__", {}).items()):
            if isinstance(value, SIZED):
                sizes[f"service:{service}.{attr}"] = len(value)
    from agents.task.path import _SINGLETON_REGISTRY
    for key, instance in list(_SINGLETON_REGISTRY.items()):
        for attr, value in list(getattr(instance, "__dict__", {}).items()):
            if isinstance(value, SIZED):
                sizes[f"singleton:{key.rsplit('.', 1)[-1]}.{attr}"] = len(value)
    return sizes


def _growers(before: dict, after: dict, cycles: int) -> set:
    """Grew by at least one per session. A lazy cache that fills once does not."""
    return {k for k, n in after.items() if n - before.get(k, 0) >= cycles}


async def _one_cycle(task_agent, i: int) -> list:
    """One create, run, evict. Returns only weakrefs: a strong one here pins the graph."""
    reply = await task_agent.chat_once("local", f"message {i}", chat_id=f"chat{i}")
    assert reply == "ok"
    session_id = task_agent._chat_sessions[task_agent._chat_key("local", f"chat{i}")]
    orchestrator = task_agent.get_orchestrator(session_id)
    agents = list(orchestrator.agents.values())
    assert agents, "the turn must have built a real Agent"
    refs = [weakref.ref(orchestrator), weakref.ref(orchestrator.controller)]
    refs += [weakref.ref(agent) for agent in agents]
    del orchestrator, agents
    await task_agent._evict_session(session_id, reason="manual")
    assert task_agent.get_orchestrator(session_id) is None
    assert session_id not in task_agent._session_last_activity
    return refs


@pytest.mark.asyncio
async def test_eviction_frees_the_graph_and_no_map_grows(monkeypatch, tmp_path):
    _temp_env(monkeypatch, tmp_path)
    with patch("modules.llm.llm_manager.LLMManager._initialize", AsyncMock()):
        from polyrob.app.bootstrap import build_cli_container
        container = await build_cli_container(log_level="ERROR")
    task_agent = container.get_agent("task_agent")

    class _DummyChat:
        model_name = "gemini-2.5-flash"

        async def ainvoke(self, *a, **k):
            raise AssertionError("the LLM must not be called: get_next_action is stubbed")

    async def _llm_for_request(_self, _request):
        return _DummyChat()

    async def _done(self, _input_messages):
        action_model = self.controller.registry.create_action_model()
        return AgentOutput(
            current_state=AgentBrain(evaluation_previous_goal="n/a", memory="m", next_goal="end"),
            action=[action_model(done={"text": "ok"})],
        )

    from agents.task.agent.service import Agent
    monkeypatch.setattr(type(task_agent), "_get_llm_for_request", _llm_for_request)
    monkeypatch.setattr(Agent, "get_next_action", _done)

    try:
        for i in range(WARM_CYCLES):  # imports, lazy caches and the first action model
            await _one_cycle(task_agent, i)
        gc.collect()
        before = {**_module_sizes(), **_service_sizes(container)}

        refs = []
        for i in range(WARM_CYCLES, WARM_CYCLES + CYCLES):
            refs += await _one_cycle(task_agent, i)
        # pytest's capture handler keeps each LogRecord, and an exc_info traceback holds the
        # frames of the Agent. No production handler keeps a record, and another test can
        # leave these loggers propagating to root, so this test must not count it.
        for handler in logging.getLogger().handlers:
            for record in getattr(handler, "records", ()):
                record.exc_info = None
        await asyncio.sleep(0)
        gc.collect()

        alive = [r() for r in refs if r() is not None]
        assert not alive, f"evicted session objects are still alive: {[type(o).__name__ for o in alive]}"

        grew = _growers(before, {**_module_sizes(), **_service_sizes(container)}, CYCLES)
        new = grew - KEPT_ON_PURPOSE - BOUNDED - KNOWN_LEAKS
        assert not new, f"a map grows per evicted session; release it at eviction: {sorted(new)}"
        fixed = KNOWN_LEAKS - grew
        assert not fixed, f"these no longer grow; remove their KNOWN_LEAKS rows: {sorted(fixed)}"
    finally:
        await task_agent.aclose()


@pytest.mark.asyncio
async def test_every_client_the_session_built_is_closed_and_no_shared_one(monkeypatch, tmp_path):
    """Builds minus closes equals the shared count: zero own clients stay open."""
    _temp_env(monkeypatch, tmp_path)
    with patch("modules.llm.llm_manager.LLMManager._initialize", AsyncMock()):
        from polyrob.app.bootstrap import build_cli_container
        container = await build_cli_container(log_level="ERROR")
    task_agent = container.get_agent("task_agent")

    built, closed = [], []

    class _Client:
        async def cleanup(self):
            closed.append(self)

    class _Own:
        model_name = "own-model"

        def __init__(self):
            self._client = _Client()
            built.append(self._client)

        async def ainvoke(self, *a, **k):
            raise AssertionError("the LLM must not be called: get_next_action is stubbed")

    shared_client = _Client()

    class _Shared(_Own):
        def __init__(self):
            self._client = shared_client

    async def _llm_for_request(_self, _request):
        return _Shared()

    async def _isolated(_self, _config, isolated=False):
        assert isolated, "an aux or swap model must be isolated"
        return _Own()

    async def _done(self, _input_messages):
        action_model = self.controller.registry.create_action_model()
        return AgentOutput(
            current_state=AgentBrain(evaluation_previous_goal="n/a", memory="m", next_goal="end"),
            action=[action_model(done={"text": "ok"})],
        )

    from agents.task.agent.service import Agent
    monkeypatch.setattr(type(task_agent), "_get_llm_for_request", _llm_for_request)
    monkeypatch.setattr(Agent, "get_next_action", _done)
    monkeypatch.setattr(Agent, "_create_llm_from_config_async", _isolated)
    monkeypatch.setattr("agents.task.constants.resolve_aux_chain",
                        lambda *a, **k: [{"provider": "gemini", "model": "aux-model"}])
    monkeypatch.setattr("core.config_policy.reflection_llm_enabled_default", lambda: True)
    llm_manager = container.get_service("llm")
    monkeypatch.setitem(llm_manager.clients, "gemini_client", shared_client)

    try:
        assert await task_agent.chat_once("local", "first", chat_id="chat") == "ok"
        session_id = task_agent._chat_sessions[task_agent._chat_key("local", "chat")]
        agent = next(iter(task_agent.get_orchestrator(session_id).agents.values()))
        assert len(built) == 2, "compaction and reflection at construction"
        agent._judge_llm = _Own()

        for model in ("swap-one", "swap-two"):  # each replaces the swap model and two aux models
            result = await agent.swap_model("gemini", model)
            assert result["ok"], result
        assert len(built) == 3 + 2 * 3
        assert closed, "a swap closes what it replaced"

        await task_agent._evict_session(session_id, reason="manual")

        assert {id(c) for c in built} == {id(c) for c in closed}, (
            f"{len(built)} built, {len(closed)} closed")
        assert len(closed) == len(built), "each one closes once"
        assert shared_client not in closed, "never close the shared client"
    finally:
        await task_agent.aclose()
