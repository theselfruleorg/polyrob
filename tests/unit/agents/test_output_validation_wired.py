"""Task 16: Output validation wired (CO-F1, CO-F10).

CO-F1: judge-backed output validation of the agent's FINAL answer was dead:
  - AgentConfig.validate_output defaulted to a hardcoded False with nothing to flip it.
  - _validate_output early-returned True (skip) for any non-browser task.
  - the run loop checked validate_output at the TOP of the NEXT iteration, but a
    done() result broke the loop before that check ever ran again — so the true
    final answer was never actually judged.
  - the judge aux model was provisioned unconditionally at construction even
    though nothing used it.

This file pins the fix: validate_output is env-defaulted (VALIDATE_OUTPUT), the
run loop judges the final done() result BEFORE breaking (bounded by the existing
`for step_num in range(max_steps)` loop — no unbounded retry), _validate_output
judges non-browser tasks too, and the judge aux model is provisioned lazily
(only on first actual use), not during construction.

CO-F10: ValidationResult.get_openai_schema referenced an un-imported `Dict` and
had zero callers (it's a local class inside _validate_output, unreachable from
outside) — deleted rather than fixing the import.
"""
from __future__ import annotations

import inspect
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.task.agent.service import Agent, AgentConfig
from agents.task.agent.views import ActionResult


# --------------------------------------------------------------------------
# Test 1 (part a): flag defaults from env (core.env SSOT), matching the style
# other AgentConfig fields use.
# --------------------------------------------------------------------------

def test_validate_output_defaults_from_env(monkeypatch):
    monkeypatch.delenv("VALIDATE_OUTPUT", raising=False)
    assert AgentConfig(task="t").validate_output is False

    monkeypatch.setenv("VALIDATE_OUTPUT", "true")
    assert AgentConfig(task="t").validate_output is True

    monkeypatch.setenv("VALIDATE_OUTPUT", "off")
    assert AgentConfig(task="t").validate_output is False


def test_validate_output_explicit_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv("VALIDATE_OUTPUT", "true")
    assert AgentConfig(task="t", validate_output=False).validate_output is False


# --------------------------------------------------------------------------
# CO-F10: the dead get_openai_schema method (missing Dict import, zero callers)
# is gone.
# --------------------------------------------------------------------------

def test_dead_get_openai_schema_removed():
    from agents.task.agent.core import output_validation

    src = inspect.getsource(output_validation)
    assert "get_openai_schema" not in src


# --------------------------------------------------------------------------
# Test 3: non-browser tasks actually get judged (no early-return short-circuit).
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_browser_task_actually_invokes_judge():
    """No live browser session (chat/coding/MCP task) must still route through
    the judge instead of the old unconditional `return True`."""
    from agents.task.agent.core.output_validation import OutputValidationMixin

    agent = OutputValidationMixin.__new__(OutputValidationMixin)
    agent.task = "answer the user's question"
    agent._last_result = [ActionResult(is_done=True, extracted_content="42", success=True)]
    agent.include_attributes = []
    agent.max_error_length = 400
    agent.use_vision = False
    agent.validate_output = True
    agent.logger = logging.getLogger("test-non-browser-validate")
    agent.get_browser_context = AsyncMock(return_value=None)  # no browser at all

    judge = MagicMock()
    judge.used = False

    def _with_structured_output(model, include_raw=True):
        judge.used = True

        class _Validator:
            async def ainvoke(self, msg):
                # Prove the judge actually saw the non-browser final answer text.
                assert any("42" in getattr(m, "content", "") for m in msg)
                return {"parsed": model(is_valid=True, reason="looks right")}

        return _Validator()

    judge.with_structured_output = _with_structured_output
    agent._judge_llm = judge

    ok = await agent._validate_output()
    assert ok is True
    assert judge.used is True, "judge must run even without a browser session"


# --------------------------------------------------------------------------
# A2: the judge prompt is task-type-neutral for non-browser tasks (no browser
# framing leaks in when there is no live browser session).
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_browser_judge_prompt_has_no_browser_framing():
    from agents.task.agent.core.output_validation import OutputValidationMixin

    agent = OutputValidationMixin.__new__(OutputValidationMixin)
    agent.task = "answer the user's question"
    agent._last_result = [ActionResult(is_done=True, extracted_content="42", success=True)]
    agent.include_attributes = []
    agent.max_error_length = 400
    agent.use_vision = False
    agent.validate_output = True
    agent.logger = logging.getLogger("test-a2-prompt")
    agent.get_browser_context = AsyncMock(return_value=None)  # no browser

    captured = {}

    judge = MagicMock()

    def _with_structured_output(model, include_raw=True):
        class _Validator:
            async def ainvoke(self, msg):
                captured["system"] = msg[0].content
                return {"parsed": model(is_valid=True, reason="ok")}

        return _Validator()

    judge.with_structured_output = _with_structured_output
    agent._judge_llm = judge

    await agent._validate_output()

    system = captured["system"].lower()
    # Task-neutral: no browser-only phrasing when there is no browser context.
    for phrase in ("interacts with a browser", "scroll", "the page", "image does not show"):
        assert phrase not in system, f"browser-only phrasing leaked into non-browser judge prompt: {phrase!r}"


# --------------------------------------------------------------------------
# Test 2: flag OFF -> the judge aux model is never provisioned during
# construction (lazy provisioning, CO-F1).
# --------------------------------------------------------------------------

def test_construction_no_longer_eagerly_provisions_judge():
    """Static guard: construction.py must not call _provision_aux_llm("judge")
    unconditionally in __init__ any more (it now only sets self._judge_llm =
    None; the real provisioning happens lazily in _validate_output)."""
    from agents.task.agent.core import construction

    src = inspect.getsource(construction.AgentConstructionMixin.__init__)
    assert 'self._provision_aux_llm("judge")' not in src
    assert "self._judge_llm = None" in src


@pytest.mark.asyncio
async def test_validate_output_flag_off_never_provisions_judge():
    """Dynamic spy: with validate_output=False (the post-construction default),
    _provision_aux_llm must never be called even if _validate_output somehow
    ran — this is the actual lazy-provisioning guard inside _validate_output."""
    from agents.task.agent.core.output_validation import OutputValidationMixin

    agent = OutputValidationMixin.__new__(OutputValidationMixin)
    agent.task = "chat task"
    agent._last_result = [ActionResult(is_done=True, extracted_content="hi", success=True)]
    agent.include_attributes = []
    agent.max_error_length = 400
    agent.use_vision = False
    agent.validate_output = False
    agent._judge_llm = None
    agent.logger = logging.getLogger("test-flag-off")
    agent.get_browser_context = AsyncMock(return_value=None)

    spy = MagicMock(return_value=MagicMock(with_structured_output=lambda *a, **k: None))
    agent._provision_aux_llm = spy

    # Even though validate_output is False here, we drive _validate_output()
    # directly (the run loop wouldn't call it at all when the flag is off) to
    # pin the provisioning guard itself: it must gate on validate_output, not
    # just "have we provisioned yet".
    main_llm = MagicMock()
    main_llm.used = False

    def _main_structured(model, include_raw=True):
        main_llm.used = True

        class _V:
            async def ainvoke(self, msg):
                return {"parsed": model(is_valid=True, reason="ok")}

        return _V()

    main_llm.with_structured_output = _main_structured
    agent.llm = main_llm

    await agent._validate_output()

    spy.assert_not_called()
    assert agent._judge_llm is None
    assert main_llm.used is True, "falls back to the main model, never a provisioned judge"


@pytest.mark.asyncio
async def test_validate_output_flag_on_lazily_provisions_judge_once():
    """When validate_output IS on, the judge is provisioned lazily on first use
    and cached — not called again on a second _validate_output() invocation."""
    from agents.task.agent.core.output_validation import OutputValidationMixin

    agent = OutputValidationMixin.__new__(OutputValidationMixin)
    agent.task = "chat task"
    agent._last_result = [ActionResult(is_done=True, extracted_content="hi", success=True)]
    agent.include_attributes = []
    agent.max_error_length = 400
    agent.use_vision = False
    agent.validate_output = True
    agent._judge_llm = None
    agent.logger = logging.getLogger("test-flag-on")
    agent.get_browser_context = AsyncMock(return_value=None)
    agent.llm = MagicMock()

    provisioned_judge = MagicMock()

    def _judge_structured(model, include_raw=True):
        class _V:
            async def ainvoke(self, msg):
                return {"parsed": model(is_valid=True, reason="ok")}

        return _V()

    provisioned_judge.with_structured_output = _judge_structured
    # P2-9: _validate_output provisions the judge via the ASYNC form now.
    spy = AsyncMock(return_value=provisioned_judge)
    agent._provision_aux_llm_async = spy

    await agent._validate_output()
    await agent._validate_output()

    spy.assert_awaited_once_with("judge")
    assert agent._judge_llm is provisioned_judge


# --------------------------------------------------------------------------
# Test 1 + 4: drive the REAL run-loop done-handling block (run_loop.py) with
# _validate_output mocked. Invalid judge -> the loop does NOT break, it
# continues (bounded by max_steps, no unbounded retry). Valid judge -> the
# loop DOES break on the first done() step.
# --------------------------------------------------------------------------

def _build_run_loop_agent(*, judge_result, max_steps):
    """Minimal Agent standing in for a real run(), stubbing everything run()
    touches except the done-handling validate_output block under test."""
    import types

    a = object.__new__(Agent)
    a.logger = logging.getLogger("run-loop-validate-test")
    a._cancelled = False
    a.task = "do a thing"
    a.initial_actions = None
    a.generate_gif = False
    a.stall_timeout_seconds = None  # skip the stall-monitor task entirely
    a.validate_output = True
    a._is_sub_agent = False
    a.agent_id = "agent_test-session"

    a.state = types.SimpleNamespace(
        n_steps=0, consecutive_failures=0, reset_llm_errors=MagicMock(),
    )

    # Every step "completes" with a done() result — the run loop's job here is
    # deciding whether to break on it or keep going per validate_output.
    done_result = [ActionResult(is_done=True, extracted_content="final answer", success=True)]

    async def _fake_step(step_info=None):
        a._last_result = done_result
        a.state.n_steps += 1

    a.step = AsyncMock(side_effect=_fake_step)
    a._last_result = []

    a._too_many_failures = MagicMock(return_value=False)
    a._handle_control_flags = AsyncMock(return_value=False)
    a._check_context_overflow = MagicMock(return_value=False)
    a._drain_user_messages = AsyncMock(return_value=[])
    a._maybe_spawn_background_review = MagicMock()

    if isinstance(judge_result, Exception):
        a._validate_output = AsyncMock(side_effect=judge_result)
    else:
        a._validate_output = AsyncMock(return_value=judge_result)

    a.telemetry_manager = MagicMock()
    a.telemetry_manager.flush_buffers = MagicMock()
    a.telemetry_manager.capture_session_end = MagicMock()
    a.telemetry_manager.capture_event = MagicMock()

    a.orchestrator = MagicMock()
    a.orchestrator.browser_manager = None
    a.orchestrator.user_id = None  # Agent.user_id is a read-only property -> orchestrator.user_id

    a.message_manager = MagicMock()
    a.message_manager.get_token_count = MagicMock(return_value=0)
    a.message_manager.model_name = "test-model"  # Agent.model_name reads message_manager.model_name

    a.task_context_manager = None
    a.usage_tracker = None
    a.register_done_callback = None

    a.history = MagicMock()
    a.history.history = []
    a.history.is_done = MagicMock(return_value=True)
    a.history.errors = MagicMock(return_value=[])

    return a, max_steps


@pytest.mark.asyncio
async def test_valid_judge_breaks_immediately_on_done():
    agent, max_steps = _build_run_loop_agent(judge_result=True, max_steps=10)

    await agent.run(max_steps=max_steps, _continue_session=True)

    assert agent.step.await_count == 1, "must break on the first done() when the judge approves"
    assert agent._validate_output.await_count == 1


@pytest.mark.asyncio
async def test_invalid_judge_continues_instead_of_breaking_and_is_bounded():
    """Bug reproduction + fix: a done() result whose judge says INVALID must
    NOT break the loop (old code broke unconditionally at the done() check
    before validation ever had a chance to run on the real final answer).
    Termination bound: the surrounding `for step_num in range(max_steps)`
    loop — an always-invalid judge still stops at max_steps, no hang."""
    max_steps = 5
    agent, _ = _build_run_loop_agent(judge_result=False, max_steps=max_steps)

    await agent.run(max_steps=max_steps, _continue_session=True)

    # Never broke early: step() ran every iteration of the bounded loop, and
    # validation was consulted every time a done() surfaced. The hard bound is
    # the surrounding `for step_num in range(max_steps)` loop itself — note
    # consecutive_failures does NOT accumulate here (each step() call has no
    # tool error, so the "step succeeded" reset at run_loop.py fires before the
    # done-block increments it again next iteration); _too_many_failures is
    # mocked to never trip precisely so this test isolates the max_steps bound.
    assert agent.step.await_count == max_steps
    assert agent._validate_output.await_count == max_steps
