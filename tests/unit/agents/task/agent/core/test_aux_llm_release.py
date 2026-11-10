"""102 M-M: a session closes each client it built, and never the shared one."""
import logging
from types import SimpleNamespace

import pytest

from agents.task.agent.core.model_swap import close_agent_llms, close_own_llm, close_session_llms


class _Client:
    def __init__(self):
        self.closed = 0

    async def cleanup(self):
        self.closed += 1


def _llm():
    return SimpleNamespace(_client=_Client())


class _Container:
    def __init__(self, **clients):
        self.manager = SimpleNamespace(clients=clients)

    def get_service(self, name):
        assert name == "llm"
        return self.manager


@pytest.mark.asyncio
async def test_the_shared_client_is_refused():
    llm = _llm()
    await close_own_llm(llm, _Container(gemini_client=llm._client))
    assert llm._client.closed == 0


@pytest.mark.asyncio
async def test_an_isolated_client_closes():
    llm = _llm()
    await close_own_llm(llm, _Container(gemini_client=_Client()))
    assert llm._client.closed == 1


@pytest.mark.asyncio
async def test_a_sync_close_and_a_failed_close_are_both_safe():
    closed = []
    await close_own_llm(SimpleNamespace(_client=SimpleNamespace(cleanup=lambda: closed.append(1))))
    assert closed == [1]

    async def _boom():
        raise RuntimeError("no")
    await close_own_llm(SimpleNamespace(_client=SimpleNamespace(cleanup=_boom)))


@pytest.mark.asyncio
async def test_agent_slots_close_once_and_clear():
    aux, judge, swap = _llm(), _llm(), _llm()
    agent = SimpleNamespace(message_manager=SimpleNamespace(aux_llm=aux), _judge_llm=judge,
                            _swap_built_llm=swap)
    await close_agent_llms(agent)
    await close_agent_llms(agent)
    assert [x._client.closed for x in (aux, judge, swap)] == [1, 1, 1]
    assert agent.message_manager.aux_llm is agent._judge_llm is agent._swap_built_llm is None


@pytest.mark.asyncio
async def test_the_root_session_end_closes_its_isolated_main_model():
    main = _llm()
    await close_session_llms(SimpleNamespace(llm=main), _Container(gemini_client=_Client()))
    assert main._client.closed == 1


@pytest.mark.asyncio
async def test_the_session_end_closes_the_first_model_a_fallback_replaced():
    first, fallback = _llm(), _llm()
    agent = SimpleNamespace(llm=fallback, _first_model=SimpleNamespace(llm=first, owned=False))
    await close_session_llms(agent, _Container(gemini_client=_Client()))
    assert (first._client.closed, fallback._client.closed) == (1, 1)


@pytest.mark.asyncio
async def test_the_session_end_keeps_the_shared_main_model():
    main = _llm()
    await close_session_llms(SimpleNamespace(llm=main), _Container(gemini_client=main._client))
    assert main._client.closed == 0


@pytest.mark.asyncio
async def test_a_sub_agent_keeps_the_model_of_its_parent():
    main = _llm()
    child = SimpleNamespace(llm=main, _is_sub_agent=True)
    await close_session_llms(child, _Container(gemini_client=_Client()))
    assert main._client.closed == 0


def test_a_sub_agent_builds_no_reflection_client(monkeypatch):
    from agents.task.agent.core.construction import AgentConstructionMixin
    monkeypatch.setattr("core.config_policy.reflection_llm_enabled_default", lambda: True)
    built = []
    host = SimpleNamespace(
        message_manager=SimpleNamespace(aux_llm=None), logger=SimpleNamespace(debug=print),
        _provision_compaction_llm=lambda: None, _provision_aux_llm=built.append, _is_sub_agent=True)
    AgentConstructionMixin._init_aux_models(host, SimpleNamespace())
    assert built == []
    host._is_sub_agent = False
    AgentConstructionMixin._init_aux_models(host, SimpleNamespace())
    assert built == ["reflection"]


@pytest.mark.asyncio
async def test_a_start_model_a_switch_replaced_closes_at_the_session_end_not_at_the_switch():
    """A child can still hold it at the switch."""
    from agents.task.agent.core.model_swap import ModelSwapMixin
    start = _llm()
    built = _llm()

    class _Agent(ModelSwapMixin):
        logger = logging.getLogger("test.retire")
        llm = start
        llm_provider = "gemini"
        model_name = "m0"
        container = None
        _swap_built_llm = None
        _first_model = None

        async def _create_llm_from_config_async(self, cfg, isolated=False):
            return built

        def adopt_active_llm(self, llm, model, provider):
            self.llm = llm

        def _after_swap(self, *args, **kwargs):
            return []

    agent = _Agent()
    assert (await agent.swap_model("gemini", "m1"))["ok"]
    assert start._client.closed == 0
    await close_session_llms(agent, _Container(gemini_client=_Client()))
    assert start._client.closed == 1 and built._client.closed >= 1


@pytest.mark.asyncio
async def test_a_retired_shared_client_stays_open():
    shared = _llm()
    agent = SimpleNamespace(llm=None, _retired_llms=[shared])
    await close_session_llms(agent, _Container(gemini_client=shared._client))
    assert shared._client.closed == 0


@pytest.mark.asyncio
async def test_a_sub_agent_closes_no_retired_model():
    retired = _llm()
    agent = SimpleNamespace(llm=None, _is_sub_agent=True, _retired_llms=[retired])
    await close_session_llms(agent, _Container(gemini_client=_Client()))
    assert retired._client.closed == 0
