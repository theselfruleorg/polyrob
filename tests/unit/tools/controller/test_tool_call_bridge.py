"""F9 (063 WS-4) — the `tool_call` bridge and the frozen provider schema list.

`load_tool` registers new actions mid-session, which grew `tools[]` and (on
Anthropic, where the wire order is tools -> system -> messages) cold-billed the
whole request. Behind TOOL_SCHEMAS_FROZEN the emitted list is pinned at the first
emit and a late tool is reached by name through `tool_call(name, arguments)` —
which dispatches through the SAME path a native call takes, so every gate fires
against the REAL target.
"""
import types

import pytest

from pydantic import BaseModel, Field


def _make_controller(tmp_path):
    import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir=str(tmp_path))
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


class _LateAction(BaseModel):
    text: str = Field(..., description="what to echo")
    times: int = Field(1, description="how many times")


def _register_late(controller, name="late_echo", record=None):
    @controller.registry.action("A late action registered after the freeze",
                                param_model=_LateAction)
    async def late_echo(params: _LateAction, execution_context=None):
        from tools.controller.types import ActionResult
        if record is not None:
            record.append(params.text)
        return ActionResult(extracted_content=params.text * params.times,
                            include_in_memory=True)
    if name != "late_echo":
        controller.registry.create_alias("late_echo", name)


def _names(schemas):
    out = []
    for s in schemas:
        out.append(s.get("name") or (s.get("function") or {}).get("name"))
    return [n for n in out if n]


# --------------------------------------------------------------------------
# the freeze
# --------------------------------------------------------------------------

def test_bridge_is_registered_and_freezes_at_the_first_emit(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    assert c.has_action("tool_call")
    assert c.registry.schemas_frozen() is False  # nothing emitted yet
    c.get_all_actions_for_provider("openai")
    assert c.registry.schemas_frozen() is True


def test_a_late_action_is_registered_but_not_emitted(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    before = _names(c.get_all_actions_for_provider("openai"))
    _register_late(c)
    after = _names(c.get_all_actions_for_provider("openai"))
    assert "late_echo" not in after
    assert sorted(after) == sorted(before)
    # ...but it IS in the registry: validation, gates and tool_describe keep working.
    assert c.registry.get_action("late_echo") is not None


def test_the_default_is_grow_no_bridge_verb_and_a_growing_tool_list(tmp_path, monkeypatch):
    """Owner decision 2026-09-23: shape (a) is OPT-IN; the default is `grow`.

    Every other test in this file sets TOOL_SCHEMAS_FROZEN explicitly, so none
    of them would notice the default flipping back. This one asserts it.
    """
    monkeypatch.delenv("TOOL_SCHEMAS_FROZEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_DEFERRED_TOOLS", raising=False)
    c = _make_controller(tmp_path)
    assert not c.has_action("tool_call")
    before = _names(c.get_all_actions_for_provider("openai"))
    assert c.registry.schemas_frozen() is False
    _register_late(c)
    after = _names(c.get_all_actions_for_provider("openai"))
    assert "late_echo" in after
    assert len(after) == len(before) + 1


def test_flag_off_restores_the_growing_tool_list(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "false")
    c = _make_controller(tmp_path)
    assert not c.has_action("tool_call")
    before = _names(c.get_all_actions_for_provider("openai"))
    _register_late(c)
    after = _names(c.get_all_actions_for_provider("openai"))
    assert "late_echo" in after
    assert len(after) == len(before) + 1


def test_tools_sha_is_unchanged_across_a_late_registration(tmp_path, monkeypatch):
    """The cache measurement: the emitted tools[] fingerprint does not move."""
    from modules.llm.prefix_stamp import compute_stamps
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    before = compute_stamps({"tools": c.get_all_actions_for_provider("anthropic")})
    _register_late(c)
    after = compute_stamps({"tools": c.get_all_actions_for_provider("anthropic")})
    assert before["tools_sha"] == after["tools_sha"]


def test_tools_sha_moves_when_the_freeze_is_off(tmp_path, monkeypatch):
    """Control case — the defect F9 removes."""
    from modules.llm.prefix_stamp import compute_stamps
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "false")
    c = _make_controller(tmp_path)
    before = compute_stamps({"tools": c.get_all_actions_for_provider("anthropic")})
    _register_late(c)
    after = compute_stamps({"tools": c.get_all_actions_for_provider("anthropic")})
    assert before["tools_sha"] != after["tools_sha"]


def test_the_frozen_set_is_part_of_the_memo_key(tmp_path, monkeypatch):
    """A late registration must not alias onto the pre-freeze memo entry."""
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    reg = c.registry
    # Emit once WITHOUT arming (the gauge path), then arm and emit.
    reg.get_schema_token_estimate("openai")
    assert reg.schemas_frozen() is False
    keys_before = len(reg._provider_schema_cache)
    reg.get_all_actions_for_provider("openai")
    assert reg.schemas_frozen() is True
    assert len(reg._provider_schema_cache) == keys_before + 1


def test_the_gauge_estimate_never_arms_the_freeze(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    c.get_tool_schema_token_estimate("openai")
    assert c.registry.schemas_frozen() is False


# --------------------------------------------------------------------------
# the bridge
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bridge_dispatch_equals_the_direct_call(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    c.get_all_actions_for_provider("openai")
    seen = []
    _register_late(c, record=seen)

    from tools.controller.tool_call_bridge import perform_tool_call
    bridged = await perform_tool_call(c, "late_echo", {"text": "ab", "times": 2})

    ActionModel = c.create_action_model()
    direct = (await c.multi_act([ActionModel(late_echo={"text": "ab", "times": 2})]))[0]

    assert bridged.extracted_content == direct.extracted_content == "abab"
    assert seen == ["ab", "ab"]  # the real action ran both times


@pytest.mark.asyncio
async def test_schema_validation_error_comes_back_as_a_result(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    _register_late(c)
    from tools.controller.tool_call_bridge import perform_tool_call
    res = await perform_tool_call(c, "late_echo", {"times": "not-an-int"})
    assert res.error is None
    assert "rejected these arguments" in (res.extracted_content or "")
    assert "tool_describe" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_unknown_name_is_a_structured_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    from tools.controller.tool_call_bridge import perform_tool_call
    res = await perform_tool_call(c, "no_such_action", {})
    assert res.error is None
    assert "no action named 'no_such_action'" in (res.extracted_content or "")
    assert "tool_search" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_bridge_cannot_call_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    from tools.controller.tool_call_bridge import perform_tool_call
    res = await perform_tool_call(c, "tool_call", {"name": "tool_call", "arguments": {}})
    assert "cannot call itself" in (res.extracted_content or "")


# --------------------------------------------------------------------------
# gate parity — the point of re-entering the ONE dispatch path
# --------------------------------------------------------------------------

class _MoneyParams(BaseModel):
    amount: str = Field("1", description="amount")


def _register_money_verb(controller):
    """Register an action the wallet-authority hook classifies as a money verb."""
    @controller.registry.action("A money verb", param_model=_MoneyParams)
    async def defi_trade(params: _MoneyParams, execution_context=None):
        from tools.controller.types import ActionResult
        return ActionResult(extracted_content="TRADED", include_in_memory=True)


@pytest.mark.asyncio
async def test_a_money_verb_is_refused_exactly_where_the_direct_call_is(tmp_path, monkeypatch):
    """The bridge must not be a way around the money gate: the hook sees the REAL
    target action, so tool_call and the direct call are refused identically."""
    from core.wallet.authority import money_action
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    c = _make_controller(tmp_path)
    c.get_all_actions_for_provider("openai")
    _register_money_verb(c)
    assert money_action("defi_trade"), "fixture must be a real money verb"

    ActionModel = c.create_action_model()
    direct = (await c.multi_act([ActionModel(defi_trade={"amount": "1"})]))[0]

    from tools.controller.tool_call_bridge import perform_tool_call
    bridged = await perform_tool_call(c, "defi_trade", {"amount": "1"})

    # Same verdict, same wording — one authorizer, reached two ways.
    assert direct.error and bridged.error
    assert direct.error == bridged.error
    assert "TRADED" not in (bridged.extracted_content or "")


@pytest.mark.asyncio
async def test_a_denylisted_action_is_refused_through_the_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    monkeypatch.setenv("POLYROB_TOOL_DENYLIST", "late_echo")
    c = _make_controller(tmp_path)
    seen = []
    _register_late(c, record=seen)
    from tools.controller.tool_call_bridge import perform_tool_call
    res = await perform_tool_call(c, "late_echo", {"text": "x"})
    assert res.error and "blocked" in res.error
    assert seen == []


# --------------------------------------------------------------------------
# what the model is TOLD (an honest instruction is half the bridge)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_tool_result_names_the_bridge_when_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    c.get_all_actions_for_provider("openai")

    async def _fake_load(ids):
        return {ids[0]: types.SimpleNamespace(actions=("a", "b"))}
    c.load_tools_from_container = _fake_load
    monkeypatch.setattr(
        "tools.tool_disclosure.resolve_tool_status",
        lambda display, **kw: types.SimpleNamespace(
            tool_id=display, status="loadable", reason="", remedy=""))

    from tools.tool_disclosure import perform_load_tool
    res = await perform_load_tool(c, "web_fetch")
    assert "tool_call(" in (res.extracted_content or "")
    assert "tool_describe" in (res.extracted_content or "")
    assert "available from the next step" not in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_load_tool_result_is_unchanged_when_the_freeze_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "false")
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)

    async def _fake_load(ids):
        return {ids[0]: types.SimpleNamespace(actions=("a", "b"))}
    c.load_tools_from_container = _fake_load
    monkeypatch.setattr(
        "tools.tool_disclosure.resolve_tool_status",
        lambda display, **kw: types.SimpleNamespace(
            tool_id=display, status="loadable", reason="", remedy=""))

    from tools.tool_disclosure import perform_load_tool
    res = await perform_load_tool(c, "web_fetch")
    assert "its actions are available from the next step." in (res.extracted_content or "")


def test_catalog_header_and_describe_name_the_bridge_when_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_SCHEMAS_FROZEN", "true")
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    c.get_all_actions_for_provider("openai")
    assert "tool_call(name=" in c.render_tool_catalog()

    monkeypatch.setattr(
        "tools.tool_search.resolve_tool_status",
        lambda display, **kw: types.SimpleNamespace(
            tool_id=display, status="loadable", reason="", remedy=""))
    from tools.tool_search import describe_tool
    text = describe_tool("filesystem", container=c.container,
                         loaded_ids=set(c.list_tools()), is_leaf=False,
                         mcp_tools={}, registry=c.registry)
    assert "tool_call(" in text
