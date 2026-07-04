"""Tests for flow-efficiency upgrades (FLOW_EFFICIENCY_ANALYSIS.md).

Each test locks in the behavior of one upgrade. Written test-first.
Bare objects (object.__new__) wired with minimal collaborators — no container/LLM.
"""

import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.task.agent.service import Agent
from agents.task.agent.views import ActionResult
from test_step_impl_characterization import _build_agent  # same dir on sys.path


# ---------------------------------------------------------------------------
# D1-b: a `done` action must be accepted on the first step (trivial finish).
# ---------------------------------------------------------------------------

def _runner_agent(n_steps: int) -> Agent:
    a = object.__new__(Agent)
    a.logger = logging.getLogger("flow-upgrade-test")
    st = MagicMock()
    st.n_steps = n_steps
    a.state = st
    ctrl = MagicMock()
    ctrl.has_action.return_value = True
    a.controller = ctrl
    return a


def _done_only_output():
    mo = MagicMock()
    action = MagicMock()
    action.model_dump.return_value = {"done": {"text": "hello"}}
    mo.action = [action]
    return mo


@pytest.mark.parametrize("n_steps", [0, 1, 2])
@pytest.mark.asyncio
async def test_done_only_accepted_on_any_step(n_steps):
    """A trivial 'say hello and finish' must validate even on the very first
    step (no premature-done gating), so it can complete in a single LLM call
    instead of being forced to a wasted second step."""
    a = _runner_agent(n_steps=n_steps)
    assert a._validate_model_output(_done_only_output()) is True


# ---------------------------------------------------------------------------
# D3-a: LLM compaction must not re-fire every step once usage stays >=85%.
# ---------------------------------------------------------------------------

def _compaction_agent(usage_pct: float) -> Agent:
    a = _build_agent(done=False, validate=True)
    mm = a.message_manager
    mm.get_context_usage_percent.return_value = usage_pct
    mm.llm_compact_history = AsyncMock()
    mm.emergency_context_prune = MagicMock()
    return a


@pytest.mark.asyncio
async def test_llm_compaction_has_step_cooldown():
    """In the 85-95% band, llm_compact_history must fire once and then NOT re-fire
    on the immediately following step (cooldown), but fire again after the gap."""
    from agents.task.constants import COMPACTION_COOLDOWN_STEPS

    a = _compaction_agent(usage_pct=90.0)
    mm = a.message_manager

    # Step N: first time over threshold -> compacts.
    a.state.n_steps = 11
    await a._prepare_step()
    assert mm.llm_compact_history.await_count == 1

    # Step N+1: still >=85% but within cooldown -> must NOT compact again.
    a.state.n_steps = 12
    await a._prepare_step()
    assert mm.llm_compact_history.await_count == 1, "compaction re-fired within cooldown"

    # Step N+cooldown: gap elapsed -> compacts again.
    a.state.n_steps = 11 + COMPACTION_COOLDOWN_STEPS
    await a._prepare_step()
    assert mm.llm_compact_history.await_count == 2


@pytest.mark.asyncio
async def test_emergency_prune_not_subject_to_cooldown():
    """At >=95% the (non-LLM) emergency prune must run every step regardless of
    any compaction cooldown -- it is the overflow safety net."""
    a = _compaction_agent(usage_pct=97.0)
    mm = a.message_manager
    a.state.n_steps = 11
    await a._prepare_step()
    a.state.n_steps = 12
    await a._prepare_step()
    assert mm.emergency_context_prune.call_count == 2
    assert mm.llm_compact_history.await_count == 0


# ---------------------------------------------------------------------------
# D4-c: the system-prompt MCP section is the full, stable form (no step-based
# compression) so the once-built system prompt stays cache-stable.
# ---------------------------------------------------------------------------

def test_mcp_section_is_full_stable_form():
    from agents.task.agent.prompts import SystemPrompt

    sp = SystemPrompt(
        action_description="actions",
        mcp_servers={"anysite": ["duckduckgo_search", "linkedin_user"]},
    )
    section = sp._get_mcp_section()
    assert "DIRECT CALLING (PREFERRED)" in section  # full, stable form
    assert "Direct call pattern" not in section  # not the (removed) compressed form


def test_system_prompt_has_no_step_number_param():
    """The dead progressive-compression knob is gone: SystemPrompt no longer
    accepts step_number (it would have made the once-built system prompt vary)."""
    import inspect
    from agents.task.agent.prompts import SystemPrompt

    assert "step_number" not in inspect.signature(SystemPrompt.__init__).parameters


# ---------------------------------------------------------------------------
# D2-b: native-tools preference must be reconciled with provider capability AND
# keep self.use_native_tools consistent (avoid mismatched native/synthetic paths).
# ---------------------------------------------------------------------------

def _native_tools_agent(user_wants: bool, provider_supports: bool) -> Agent:
    a = object.__new__(Agent)
    a.logger = logging.getLogger("flow-upgrade-test")
    a.use_native_tools = user_wants
    ctrl = MagicMock()
    ctrl.supports_native_tools.return_value = provider_supports
    a.controller = ctrl
    return a


def test_native_tools_downgraded_when_provider_lacks_support():
    """User wants native but provider can't: effective is False AND the agent's
    own flag is updated to match (so _add_tool_messages and _call_llm agree)."""
    a = _native_tools_agent(user_wants=True, provider_supports=False)
    effective = a._reconcile_native_tools("deepseek")
    assert effective is False
    assert a.use_native_tools is False


def test_native_tools_kept_when_provider_supports():
    a = _native_tools_agent(user_wants=True, provider_supports=True)
    assert a._reconcile_native_tools("anthropic") is True
    assert a.use_native_tools is True


def test_native_tools_respect_user_opt_out():
    a = _native_tools_agent(user_wants=False, provider_supports=True)
    assert a._reconcile_native_tools("anthropic") is False
    assert a.use_native_tools is False


# ---------------------------------------------------------------------------
# Chat schema: loop-intervention messages must be tagged as control content
# (origin=INTERVENTION + enveloped), not masquerade as a plain user turn.
# ---------------------------------------------------------------------------

from collections import deque


def _intervention_agent() -> Agent:
    a = object.__new__(Agent)
    a.logger = logging.getLogger("flow-upgrade-test")
    a.message_manager = MagicMock()
    a._last_result = []
    a._action_repetition_counter = 5
    a._previous_actions = deque(["x", "y"], maxlen=10)
    st = MagicMock()
    st.loop_warning_count = 0
    a.state = st
    return a


def test_loop_intervention_message_is_tagged_control_content():
    from modules.llm.messages import MessageOrigin

    a = _intervention_agent()
    a._trigger_loop_intervention("Action repeated 5 times")

    assert a.message_manager.add_message.called
    msg = a.message_manager.add_message.call_args.args[0]
    assert msg.origin == MessageOrigin.INTERVENTION
    assert "<system-directive>" in msg.content


# ---------------------------------------------------------------------------
# D1-a (seed): the first tool-free turn is a tolerated planning turn (gentle
# nudge, not an error result); beyond the allowance it reverts to error framing.
# ---------------------------------------------------------------------------

def _intervene_agent():
    a = _build_agent(done=False, validate=False)  # _validate_model_output -> False
    a._validate_model_output = lambda mo: False
    a._empty_action_counter = 0
    return a


def test_first_tool_free_turn_is_a_planning_turn(monkeypatch):
    import agents.task.constants as C
    monkeypatch.setattr(C, "ALLOWED_REASONING_TURNS", 1)

    a = _intervene_agent()
    mo = MagicMock()
    mo.action = []
    cont = a._validate_and_intervene(mo)

    assert cont is False  # step still ends (no action executed)
    # ...but it is NOT framed as an error: last_result carries no error.
    assert a._last_result and a._last_result[0].error is None


def test_beyond_allowance_is_error(monkeypatch):
    import agents.task.constants as C
    monkeypatch.setattr(C, "ALLOWED_REASONING_TURNS", 1)

    a = _intervene_agent()
    a._empty_action_counter = 1  # already used the one allowed planning turn
    mo = MagicMock()
    mo.action = []
    a._validate_and_intervene(mo)

    assert a._last_result and a._last_result[0].error is not None
