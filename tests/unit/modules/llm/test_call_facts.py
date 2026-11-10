"""Proposal 106 cause 1: one per-call result. A client that sessions share holds no fact of
one call: each usage record reads the message its own call returned."""
import asyncio
import types

import pytest

from modules.llm.adapters import AnthropicAdapter, OpenRouterAdapter
from modules.llm.aux_metering import meter_aux_llm
from modules.llm.adapters import DeepSeekAdapter
from modules.llm.call_scope import (
    begin_call, session_reasoning, set_foundation_len, set_session_tag, set_step_context,
)
from modules.llm.messages import AIMessage, HumanMessage, SystemMessage
from modules.llm.openrouter_client import OpenRouterClient
from modules.llm.openrouter_reasoning import (
    apply_request_extras, reasoning_mandatory,
)
from modules.llm.openrouter_routing import apply_provider_routing, provider_routing_extra_body
from modules.llm.call_result import read_stamps
from modules.llm.prefix_stamp import context_stamps

READ = {"type": "function", "function": {"name": "read_file", "parameters": {}}}


def _logger():
    quiet = lambda *a, **k: None  # noqa: E731
    return types.SimpleNamespace(debug=quiet, info=quiet, warning=quiet, error=quiet,
                                 isEnabledFor=lambda *a: False)


def _openrouter(sdk, model="deepseek/deepseek-v4.1-flash"):
    c = OpenRouterClient.__new__(OpenRouterClient)
    c.model_type = model
    c.max_tokens = 128
    c.temperature = 0.2
    c.last_response = None
    c._initialized = True
    c.logger = _logger()
    c._client = sdk
    return c


def _completion(tag, tokens):
    msg = types.SimpleNamespace(content=f"answer {tag}", tool_calls=None)
    usage = types.SimpleNamespace(
        prompt_tokens=tokens, completion_tokens=tokens // 10, total_tokens=tokens + tokens // 10,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=tokens // 2, cache_write_tokens=7),
        completion_tokens_details=types.SimpleNamespace(reasoning_tokens=tokens // 20),
        cost=tokens / 1_000_000)
    return types.SimpleNamespace(
        id=f"gen-{tag}", provider=f"Upstream{tag}", usage=usage,
        choices=[types.SimpleNamespace(message=msg, finish_reason="length" if tag == "A" else "stop")])


class _Completions:
    """Session A's request waits until session B's response has landed on the client."""

    def __init__(self):
        self.b_done = asyncio.Event()
        self.calls = []

    async def create(self, **params):
        self.calls.append(params)
        tag = params["messages"][-1]["content"]
        if tag == "A":
            await self.b_done.wait()
            return _completion("A", 2000)
        return _completion("B", 1000)


class _Tracker:
    def __init__(self):
        self.rows = {}

    async def record_llm_usage(self, **kwargs):
        self.rows[kwargs["session_id"]] = kwargs


def test_two_sessions_on_one_client_each_record_their_own_values():
    async def _session(adapter, tracker, completions, tag, ctx):
        set_step_context(context_stamps({"slots": {"foundation": ctx}, "limit": 100, "pct": 1.0}))
        set_foundation_len(1)
        response = await adapter.ainvoke(
            [SystemMessage(content=f"system {tag}"), HumanMessage(content=f"row {tag}"),
             HumanMessage(content=tag)],
            tools=[READ] if tag == "A" else [READ, {"type": "function", "function": {"name": "x"}}])
        if tag == "B":
            completions.b_done.set()
        else:
            await asyncio.sleep(0)
        # The other session's call has landed on the client by the time A bills.
        await asyncio.sleep(0)
        return response

    async def _run():
        completions = _Completions()
        sdk = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
        client = _openrouter(sdk)
        adapter = OpenRouterAdapter(client=client, model_name=client.model_type)
        tracker = _Tracker()
        a, b = await asyncio.gather(_session(adapter, tracker, completions, "A", 11),
                                    _session(adapter, tracker, completions, "B", 22))
        client.last_response = _completion("C", 5)   # a later call of a third session
        for sid, message in (("sa", a), ("sb", b)):
            await meter_aux_llm(usage_tracker=tracker, user_id="u", session_id=sid, agent_id="x",
                                llm=adapter, response=message, duration_seconds=0.1,
                                component="judge", purpose="t")
        return a, b, tracker

    a, b, tracker = asyncio.run(_run())
    row_a, row_b = tracker.rows["sa"], tracker.rows["sb"]
    assert row_a["input_tokens"] == 2000 and row_b["input_tokens"] == 1000
    assert row_a["cached_tokens"] == 1000 and row_b["cached_tokens"] == 500
    assert row_a["billed_cost_usd"] == pytest.approx(0.002) and row_b["billed_cost_usd"] == pytest.approx(0.001)
    assert row_a["request_id"].endswith("gen-A") and row_b["request_id"].endswith("gen-B")
    meta_a, meta_b = row_a["metadata"], row_b["metadata"]
    assert meta_a["upstream"] == "UpstreamA" and meta_b["upstream"] == "UpstreamB"
    assert meta_a["reasoning_tokens"] == 100 and meta_b["reasoning_tokens"] == 50
    assert meta_a["prefix_sha"] != meta_b["prefix_sha"]
    assert meta_a["tools_sha"] != meta_b["tools_sha"]
    assert meta_a["foundation_sha"] != meta_b["foundation_sha"]
    assert a.call_facts.finish_reason == "length" and b.call_facts.finish_reason == "stop"
    assert read_stamps(a)["ctx_foundation"] == 11 and read_stamps(b)["ctx_foundation"] == 22
    assert a.call_facts.billed_source == "provider"


def test_a_step_context_is_not_consumed_by_an_aux_call_of_another_task():
    async def _aux():
        return begin_call().context

    async def _run():
        set_step_context(context_stamps({"slots": {"foundation": 5}}))
        seen = await asyncio.create_task(_aux())
        return seen, begin_call().context

    try:
        seen, own = asyncio.run(_run())
    finally:
        set_step_context(None)
    assert seen == own == {"ctx_foundation": 5}


def test_concurrent_first_calls_build_one_sdk_client():
    """S7."""
    built = []

    async def _setup():
        built.append(1)
        await asyncio.sleep(0)

    async def _run():
        client = _openrouter(None)
        client._initialized = False
        client._skip_validate = True
        client._validate_llm_config = lambda: None
        client._setup_client = _setup
        await asyncio.gather(client._initialize(), client._initialize())
        return client

    assert asyncio.run(_run())._initialized is True
    assert len(built) == 1


# ── L6: the shared client maps an alias ─────────────────────────────────────

def test_an_openrouter_alias_goes_out_as_its_row_name():
    assert _openrouter(None, "glm-5.3").model_type == "z-ai/glm-5.3"
    assert _openrouter(None, "vendor/not-in-the-catalog").model_type == "vendor/not-in-the-catalog"


def test_a_subclass_seat_keeps_the_id_it_was_given():
    from modules.llm.nvidia_client import NvidiaClient
    c = NvidiaClient.__new__(NvidiaClient)
    c.model_type = "glm-5.3"
    assert c.model_type == "glm-5.3"


def test_reasoning_mandatory_sees_the_canonical_name():
    assert reasoning_mandatory("glm-5.3") and reasoning_mandatory("z-ai/glm-5.3")
    assert not reasoning_mandatory("x-ai/grok-4"), "a model with no row is learned from its first 400"


# ── C11, tool_choice ────────────────────────────────────────────────────────

def test_openrouter_cache_write_tokens_are_read():
    client = _openrouter(None)
    client.last_response = _completion("A", 2000)
    assert client._extract_usage_data()["cache_creation_tokens"] == 7


def test_an_explicit_tool_choice_reaches_the_wire_with_tools():
    completions = _Completions()
    sdk = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    adapter = OpenRouterAdapter(client=_openrouter(sdk), model_name="m")
    done = {"type": "function", "function": {"name": "done", "parameters": {}}}
    completions.b_done.set()
    asyncio.run(adapter.ainvoke([HumanMessage(content="B")], tools=[done], tool_choice="none"))
    assert completions.calls[-1]["tool_choice"] == "none"
    asyncio.run(adapter.ainvoke([HumanMessage(content="B")], tools=[done]))
    assert completions.calls[-1]["tool_choice"] == "required"


def test_a_tool_choice_with_no_tools_stays_off_the_wire():
    adapter = OpenRouterAdapter(client=_openrouter(None), model_name="m")
    assert "tool_choice" not in adapter._build_generation_params(None, {"tools": None, "tool_choice": "none"})


def test_anthropic_gets_its_own_tool_choice_shape():
    assert AnthropicAdapter._wire_tool_choice("none") == {"type": "none"}
    assert AnthropicAdapter._wire_tool_choice("required") == {"type": "any"}


# ── W4: per-model order, session id ─────────────────────────────────────────

def _order(model):
    body = provider_routing_extra_body(model) or {}
    return body.get("provider", {}).get("order")


def test_plain_order_serves_every_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_ORDER", "UpA, UpB")
    assert _order("any/model") == ["UpA", "UpB"]


def test_per_model_order_on_the_same_flag(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_ORDER", "m/one=UpA,UpB;z-ai/glm-5.3=UpC")
    assert _order("m/one") == ["UpA", "UpB"]
    assert _order("z-ai/glm-5.3") == ["UpC"]
    assert _order("m/other") is None


def test_a_plain_list_beside_the_map_is_the_default_and_an_alias_reads_its_row(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_ORDER", "glm-5.3=UpC;UpD")
    assert _order("z-ai/glm-5.3") == ["UpC"]
    assert _order("m/other") == ["UpD"]


def test_routing_reads_the_model_of_the_request(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_ORDER", "m/one=UpA")
    params = {"model": "m/one"}
    apply_provider_routing(params)
    assert params["extra_body"]["provider"]["order"] == ["UpA"]


def _extras(model, client=None):
    params = {"model": model, "messages": []}
    apply_request_extras(params, client or types.SimpleNamespace(SENDS_SESSION_ID=True), 100)
    return params["extra_body"]


def test_session_id_goes_out_hashed_on_the_zai_family_only():
    set_session_tag("session-raw-id")
    try:
        sent = _extras("z-ai/glm-5.3")["session_id"]
        assert sent and "session-raw-id" not in sent
        assert "session_id" not in _extras("deepseek/deepseek-v4-pro")
        assert "session_id" not in _extras("z-ai/glm-5.3", types.SimpleNamespace(SENDS_SESSION_ID=False))
    finally:
        set_session_tag(None)
    assert "session_id" not in _extras("z-ai/glm-5.3")


# ── L2, L1, L7, L8: native DeepSeek ─────────────────────────────────────────

def _deepseek(model="deepseek-flash"):
    from modules.llm.deepseek_client import DeepSeekClient
    c = object.__new__(DeepSeekClient)
    c.logger = _logger()
    c.model_type = model
    return c


def test_a_text_only_assistant_turn_replays_its_reasoning():
    adapter = DeepSeekAdapter(client=_deepseek(), model_name="deepseek-flash")
    with_reasoning = AIMessage(content="text")
    with_reasoning.additional_kwargs.update(reasoning_content="plan", reasoning_seat="deepseek-native")
    out = adapter._convert_to_client_messages(
        [HumanMessage(content="go"), with_reasoning, AIMessage(content="other model")])
    assert out[1]["reasoning_content"] == "plan" and out[2]["reasoning_content"] == ""
    assert "reasoning_content" not in out[0]


@pytest.mark.parametrize("level,thinking,effort", [
    ("off", "disabled", None), ("on", "enabled", None), ("low", "enabled", "low"),
    ("medium", "enabled", "high"), ("high", "enabled", "high"),
])
def test_the_owners_level_maps_to_deepseek_fields(level, thinking, effort):
    params = {"model": "deepseek-flash", "messages": []}
    with session_reasoning(level):
        _deepseek().request_extras(params)
    assert params["extra_body"]["thinking"] == {"type": thinking}
    assert params.get("reasoning_effort") == effort


def test_no_level_sends_no_thinking_field():
    params = {"model": "deepseek-flash", "messages": []}
    _deepseek().request_extras(params)
    assert "extra_body" not in params


def test_a_token_level_on_deepseek_is_refused_so_the_display_stays_true():
    from agents.task.model_switch_service import _token_level_refused
    assert _token_level_refused("deepseek", "5000")
    assert _token_level_refused("deepseek", "high") is None
    assert _token_level_refused("openrouter", "5000") is None


def _usage():
    return types.SimpleNamespace(prompt_tokens=1000, completion_tokens=10, total_tokens=1010,
                                 prompt_cache_hit_tokens=500, prompt_tokens_details=None)


def test_a_deepseek_id_with_no_exact_row_has_no_charge():
    client = _deepseek("deepseek-v9")
    client.last_response = types.SimpleNamespace(usage=_usage())
    out = client._extract_usage_data()
    assert out["billed_cost_usd"] is None and "billed_source" not in out


def test_a_deepseek_charge_is_labelled_an_estimate():
    client = _deepseek()
    client.last_response = types.SimpleNamespace(usage=_usage())
    out = client._extract_usage_data()
    assert out["billed_cost_usd"] > 0 and out["billed_source"] == "estimate"
    usage = OpenRouterAdapter._usage_metadata_from(out)
    assert usage["billed_source"] == "estimate"


# ── L13, the adapter part ───────────────────────────────────────────────────

def test_the_responses_no_tools_path_reads_usage_and_cut():
    from modules.llm.responses_client import ResponsesCompatClient
    client = object.__new__(ResponsesCompatClient)
    client.last_response = types.SimpleNamespace(
        id="resp-1", incomplete_details=types.SimpleNamespace(reason="max_output_tokens"),
        usage=types.SimpleNamespace(input_tokens=9, output_tokens=3, total_tokens=12))
    adapter = OpenRouterAdapter(client=client, model_name="m")
    assert adapter._plain_call_usage()["input_tokens"] == 9
    from modules.llm.call_result import call_facts
    facts = call_facts(client.last_response, adapter._plain_call_usage(), None)
    assert facts.finish_reason == "max_output_tokens" and facts.output_tokens == 3
