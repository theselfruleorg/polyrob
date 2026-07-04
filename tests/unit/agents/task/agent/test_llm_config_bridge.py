"""Verifies the sync->async LLM-creation bridge no longer uses asyncio.run (PR9 item #1).

_create_llm_from_config must work both from a plain sync context and from inside a
running event loop, routing through a fresh standard loop either way.
"""

import asyncio
import logging

import pytest

from agents.task.agent.service import Agent


def _bare_agent():
    a = object.__new__(Agent)
    a.logger = logging.getLogger("test_llm_bridge")

    async def fake_async(cfg, isolated=False):
        return ("LLM", cfg)

    a._create_llm_from_config_async = fake_async
    return a


def test_bridge_from_sync_context():
    # No running loop in this thread.
    a = _bare_agent()
    assert a._create_llm_from_config({"model": "m1"}) == ("LLM", {"model": "m1"})
    # And the thread's event loop wasn't left as a closed loop.
    try:
        loop = asyncio.get_event_loop()
        assert loop is None or not loop.is_closed()
    except RuntimeError:
        pass  # no loop installed — also fine


@pytest.mark.asyncio
async def test_bridge_from_running_loop():
    # Called from inside a running loop -> must use the worker-thread path,
    # NOT crash (which asyncio.run would).
    a = _bare_agent()
    assert a._create_llm_from_config({"model": "m2"}) == ("LLM", {"model": "m2"})


def test_no_asyncio_run_in_source():
    import agents.task.agent.service as svc
    src = __import__("inspect").getsource(svc._create_llm_from_config) if hasattr(svc, "_create_llm_from_config") else None
    # method lives on the class
    src = __import__("inspect").getsource(Agent._create_llm_from_config)
    assert "asyncio.run(" not in src
