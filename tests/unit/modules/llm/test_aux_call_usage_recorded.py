"""A-4 / D-5 (review 2026-10-14): aux LLM calls recorded 0 tokens / $0.

* The adapter's no-tools path (compaction, judge, triage, reflection) built the
  AIMessage WITHOUT usage_metadata, so `extract_token_usage` found nothing.
* `meter_aux_llm` dropped `billed_cost_usd` / `cache_discount_usd`, so even a
  measured aux call stored the catalog estimate.
* The command-guard triage call was not metered at all.
"""
import asyncio
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.llm.adapters import OpenAIAdapter
from modules.llm.aux_metering import meter_aux_llm
from modules.llm.messages import HumanMessage
from modules.llm.usage_extract import extract_token_usage


class _Client:
    def __init__(self, usage):
        self.model_type = "gpt-x"
        self.last_response = None
        self._usage = usage
        self.generate_response = AsyncMock(return_value="summary")

    def _extract_usage_data(self):
        return dict(self._usage)


class _Tracker:
    def __init__(self):
        self.calls = []

    async def record_llm_usage(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace()


@pytest.mark.asyncio
async def test_no_tools_path_carries_usage_metadata():
    client = _Client({"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280,
                      "cached_tokens": 1000, "billed_cost_usd": 0.0021})
    a = OpenAIAdapter(client=client, model_name="gpt-x")
    msg = await a.ainvoke([HumanMessage(content="summarise")], tools=None)
    usage = extract_token_usage(msg, "openai")
    assert usage["prompt_tokens"] == 1200 and usage["completion_tokens"] == 80
    assert usage["cached_tokens"] == 1000
    assert usage["billed_cost_usd"] == pytest.approx(0.0021)


@pytest.mark.asyncio
async def test_no_tools_path_without_usage_stays_none():
    client = _Client({"prompt_tokens": None, "completion_tokens": None, "total_tokens": None})
    a = OpenAIAdapter(client=client, model_name="gpt-x")
    msg = await a.ainvoke([HumanMessage(content="summarise")], tools=None)
    assert not msg.usage_metadata


def test_meter_aux_passes_billed_cost_and_cache_discount():
    tr = _Tracker()
    resp = SimpleNamespace(usage_metadata={"input_tokens": 500, "output_tokens": 40,
                                           "billed_cost_usd": 0.0042, "cache_discount_usd": 0.001})
    asyncio.run(meter_aux_llm(usage_tracker=tr, user_id="u1", session_id="s1", agent_id="a1",
                              llm=SimpleNamespace(model_name="m"), response=resp,
                              duration_seconds=0.1, component="judge", purpose="x"))
    c = tr.calls[0]
    assert c["billed_cost_usd"] == pytest.approx(0.0042)
    assert c["cache_discount_usd"] == pytest.approx(0.001)


def test_meter_aux_records_upstream_but_never_the_step_context():
    from modules.llm.prefix_stamp import stamp_context
    tr = _Tracker()
    client = SimpleNamespace(last_response={"provider": "DeepInfra", "usage": {}})
    llm = SimpleNamespace(model_name="m", _client=client)
    stamp_context(llm, {"slots": {"foundation": 5}, "limit": 100, "pct": 5.0})
    resp = SimpleNamespace(usage_metadata={"input_tokens": 5, "output_tokens": 1})
    asyncio.run(meter_aux_llm(usage_tracker=tr, user_id="u1", session_id="s1", agent_id="a1",
                              llm=llm, response=resp, duration_seconds=0.1,
                              component="judge", purpose="x"))
    meta = tr.calls[0]["metadata"]
    assert meta["upstream"] == "DeepInfra"
    assert not any(k.startswith("ctx_") for k in meta)
    # the step's context stamp is still there for the step's own billing row
    from modules.llm.prefix_stamp import read_stamps
    assert read_stamps(llm)["ctx_foundation"] == 5


@pytest.mark.asyncio
async def test_shell_triage_is_metered(monkeypatch):
    from tools.controller import command_guard_hook as cg
    seen = []

    async def _fake_meter(**kw):
        seen.append(kw)

    import modules.llm.aux_metering as am
    monkeypatch.setattr(am, "meter_aux_llm", _fake_meter)

    class _LLM:
        async def ainvoke(self, msgs):
            return types.SimpleNamespace(content="APPROVE", usage_metadata={"input_tokens": 9})

    tracker = object()
    orch = SimpleNamespace(agents={}, usage_tracker=tracker)
    factory = cg.orchestrator_llm_factory(orch)
    llm = _LLM()

    async def _f():
        return llm
    _f.orchestrator = factory.orchestrator
    ctx = SimpleNamespace(user_id="u1", session_id="s1", agent_id="a1")
    assert await cg.smart_triage("docker ps", "risky", _f, ctx) == "approve"
    assert len(seen) == 1
    kw = seen[0]
    assert kw["usage_tracker"] is tracker and kw["user_id"] == "u1"
    assert kw["session_id"] == "s1" and kw["component"] == "command_guard"
    assert kw["llm"] is llm


@pytest.mark.asyncio
async def test_shell_triage_metering_failure_never_changes_the_verdict(monkeypatch):
    from tools.controller import command_guard_hook as cg
    import modules.llm.aux_metering as am

    async def _boom(**kw):
        raise RuntimeError("tracker down")
    monkeypatch.setattr(am, "meter_aux_llm", _boom)

    class _LLM:
        async def ainvoke(self, msgs):
            return types.SimpleNamespace(content="DENY")

    async def _f():
        return _LLM()
    assert await cg.smart_triage("rm -rf /x", "risky", _f) == "deny"
