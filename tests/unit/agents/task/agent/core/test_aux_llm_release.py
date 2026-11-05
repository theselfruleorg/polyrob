"""102 M-M: a session closes each client it built, and never the shared one."""
from types import SimpleNamespace

import pytest

from agents.task.agent.core.model_swap import close_agent_llms, close_own_llm


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
