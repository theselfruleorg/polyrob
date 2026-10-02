"""F9 shape (b) — a late action is EMITTED, marked `defer_loading` (2026-09-23).

In `deferred` mode the Registry still records the action set at the first emit,
but a later registration is no longer skipped: it joins `tools[]` carrying
`"defer_loading": true`, which tells Anthropic the tool is known to the request
and must NOT be loaded into the model's context. Every other provider gets the
same action with no such key (it is an Anthropic-only shape).

⚠️ NOT VERIFIABLE OFFLINE: whether the PROVIDER really leaves the cached prefix
intact when a deferred tool is added is a property of Anthropic's server. This
tree has no Anthropic key, so what is asserted here is the bytes we build — the
stamp over the NON-deferred tools, which is the part the contract says is the
prefix.
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


def _register_late(controller, name="late_echo"):
    @controller.registry.action("A late action registered after the freeze",
                                param_model=_LateAction)
    async def late_echo(params: _LateAction, execution_context=None):
        from tools.controller.types import ActionResult
        return ActionResult(extracted_content=params.text, include_in_memory=True)


def _by_name(schemas):
    out = {}
    for s in schemas:
        name = s.get("name") or (s.get("function") or {}).get("name")
        if name:
            out[name] = s
    return out


@pytest.fixture(autouse=True)
def _deferred_defaults(monkeypatch):
    monkeypatch.delenv("TOOL_SCHEMAS_FROZEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_DEFERRED_TOOLS", raising=False)


def test_a_late_action_is_emitted_with_defer_loading_for_anthropic(tmp_path):
    c = _make_controller(tmp_path)
    before = _by_name(c.get_all_actions_for_provider("anthropic"))
    assert c.registry.schemas_frozen() is True  # armed unconditionally in this mode
    _register_late(c)
    after = _by_name(c.get_all_actions_for_provider("anthropic"))
    assert "late_echo" in after
    assert after["late_echo"]["defer_loading"] is True
    # Nothing that existed before the freeze is deferred.
    assert all("defer_loading" not in after[n] for n in before)


def test_openai_gets_the_same_action_without_the_key(tmp_path):
    """`defer_loading` is an Anthropic-only shape — never leak it elsewhere."""
    c = _make_controller(tmp_path)
    c.get_all_actions_for_provider("openai")
    _register_late(c)
    schemas = c.get_all_actions_for_provider("openai")
    by_name = _by_name(schemas)
    assert "late_echo" in by_name
    assert all("defer_loading" not in s for s in schemas)
    assert all("defer_loading" not in (s.get("function") or {}) for s in schemas)


def test_the_non_deferred_prefix_stamp_does_not_move(tmp_path):
    """The cache measurement we CAN make offline.

    The contract says adding a deferred tool is not an edit to the cached
    prefix, so the prefix is the non-deferred subset — and that subset's
    `tools_sha` must be identical before and after a late registration.
    """
    from modules.llm.prefix_stamp import compute_stamps
    c = _make_controller(tmp_path)
    before = c.get_all_actions_for_provider("anthropic")
    before_stamp = compute_stamps({"tools": before})
    _register_late(c)
    after = c.get_all_actions_for_provider("anthropic")
    non_deferred = [s for s in after if not s.get("defer_loading")]
    assert compute_stamps({"tools": non_deferred}) == before_stamp
    # ...and the deferred tool really is the only difference.
    assert len(after) == len(non_deferred) + 1


def test_late_action_names_reports_exactly_the_late_ones(tmp_path):
    c = _make_controller(tmp_path)
    c.get_all_actions_for_provider("anthropic")
    assert c.registry.late_action_names() == ()
    _register_late(c)
    assert c.registry.late_action_names() == ("late_echo",)
    # An alias of a late action is the SAME tool, named twice — not a second one.
    c.registry.create_alias("late_echo_alias", "late_echo")
    assert c.registry.late_action_names() == ("late_echo",)


def test_grow_mode_records_no_freeze_and_reports_no_late_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_DEFERRED_TOOLS", "false")
    c = _make_controller(tmp_path)
    c.get_all_actions_for_provider("anthropic")
    assert c.registry.schemas_frozen() is False
    _register_late(c)
    assert c.registry.late_action_names() == ()
    assert "late_echo" in _by_name(c.get_all_actions_for_provider("anthropic"))


def test_the_mode_is_part_of_the_memo_key(tmp_path, monkeypatch):
    """A flag flip must never be served the other mode's memoized bytes."""
    c = _make_controller(tmp_path)
    deferred = c.get_all_actions_for_provider("anthropic")
    _register_late(c)
    deferred_after = c.get_all_actions_for_provider("anthropic")
    monkeypatch.setenv("ANTHROPIC_DEFERRED_TOOLS", "false")
    grown = c.get_all_actions_for_provider("anthropic")
    assert any(s.get("defer_loading") for s in deferred_after)
    assert all("defer_loading" not in s for s in grown)
    assert len(grown) == len(deferred) + 1
