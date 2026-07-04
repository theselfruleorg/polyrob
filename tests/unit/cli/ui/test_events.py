"""Tests for cli.ui.events — feed dict → typed RenderEvent normalisation.

The feed dicts in this file are constructed to mirror exactly what each
formatter class in ``agents/task/telemetry/formatters.py`` produces.  This is
the primary regression guard against the wrong-key bug class documented in the
proposal (§2.2): if a formatter changes its key names, these tests break.

Key invariants under test:
- ``cost_estimate`` (not ``cost``) flows through LLMCall.cost_estimate.
- ``token_count`` / ``prompt_tokens`` / ``completion_tokens`` (not
  ``total_tokens`` / ``tokens``) flow through LLMCall.
- ``error_message`` (not ``message``) flows through ErrorEvent.
- ``tool_name`` / ``action_name`` (not ``tool`` / ``name``) flow through ToolExec.
- Unknown / passthrough types never crash.
"""

import pytest

from cli.ui.events import (
    ErrorEvent,
    Info,
    IterationDone,
    LLMCall,
    SessionDone,
    SessionStart,
    Step,
    ToolExec,
    normalize,
)


# ---------------------------------------------------------------------------
# Helpers: build feed dicts that mirror real formatter output
# ---------------------------------------------------------------------------

def _session_start(
    task="test task",
    model_name="gemini-2.5-flash",
    agent_id="executor_abc123",
    use_vision=False,
) -> dict:
    """Mirror SessionStartFormatter.format() output."""
    return {
        "type": "session_start",
        "timestamp": 1234567890.0,
        "datetime": "2026-06-11T12:00:00",
        "data": {
            "session_id": "abc123",
            "task": task,
            "model_name": model_name,
            "agent_id": agent_id,
            "agent_type": "executor",
            "use_vision": use_vision,
            "internal_planning": True,
        },
    }


def _llm_request(
    model_name="gemini-2.5-flash",
    prompt_tokens=1024,
    completion_tokens=256,
    token_count=1280,
    cost_estimate=0.000512,
    duration_seconds=1.23,
    success=True,
    provider="gemini",
) -> dict:
    """Mirror LLMRequestFormatter.format() output — the real keys matter here."""
    return {
        "type": "llm_request",
        "timestamp": 1234567890.0,
        "datetime": "2026-06-11T12:00:01",
        "data": {
            "component": "agent",
            "purpose": "step",
            "model_name": model_name,
            "provider": provider,
            "duration_seconds": duration_seconds,
            "success": success,
            # Real keys (NOT "cost", "total_tokens", or "tokens"):
            "token_count": token_count,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_estimate": cost_estimate,
            "agent_id": "executor_abc123",
            "request_id": "req-001",
        },
    }


def _step(
    step=3,
    reasoning="I should read the file first.",
    memory="tracking auth refactor",
    actions=None,
) -> dict:
    """Mirror AgentStepFormatter.format() output."""
    if actions is None:
        actions = [
            {
                "action_type": "filesystem_read_file",
                "name": "read_file",
                "service": "filesystem",
                "params": {"file_path": "config.py"},
            }
        ]
    return {
        "type": "step",
        "step": step,
        "timestamp": 1234567890.0,
        "datetime": "2026-06-11T12:00:02",
        "data": {
            "actions": actions,
            "errors": [],
            "consecutive_failures": 0,
            "agent_name": "Executor",
            "agent_type": "executor",
            "reasoning": reasoning,
            "context": {
                "outputs": {
                    "memory": memory,
                    "reasoning": reasoning,
                }
            },
            "iteration": step,
            "iteration_type": "mixed",
            "iteration_status": "active",
        },
    }


def _tool_execution(
    tool_name="filesystem",
    action_name="read_file",
    success=True,
    duration_seconds=0.12,
    error=None,
    step=3,
) -> dict:
    """Mirror ToolExecutionFormatter.format() output."""
    return {
        "type": "tool_execution",
        "step": step,
        "timestamp": 1234567890.0,
        "datetime": "2026-06-11T12:00:03",
        "data": {
            "tool_name": tool_name,
            "action_name": action_name,
            "success": success,
            "duration_seconds": duration_seconds,
            "error": error,
            "result_size": 512,
            "result_truncated": False,
            "result_preview": "file content...",
            "parameters": {"file_path": "config.py"},
        },
    }


def _iteration_complete(
    iteration=3,
    step=3,
    iteration_status="completed",
    is_done=False,
) -> dict:
    """Mirror IterationCompleteFormatter.format() output."""
    return {
        "type": "iteration_complete",
        "timestamp": 1234567890.0,
        "datetime": "2026-06-11T12:00:04",
        "data": {
            "iteration": iteration,
            "step": step,
            "iteration_type": "mixed",
            "iteration_status": iteration_status,
            "reasoning_summary": "completed the read",
            "actions": [],
            "action_results": [],
            "files_created": [],
            "files_modified": [],
            "files_read": ["config.py"],
            "files_deleted": [],
            "success": True,
            "error": None,
            "is_done": is_done,
            "duration_seconds": 0.5,
        },
    }


def _error(
    error_message="Something went wrong",
    error_type="ToolError",
    step=2,
) -> dict:
    """Mirror ErrorFormatter.format() output."""
    return {
        "type": "error",
        "step": step,
        "timestamp": 1234567890.0,
        "datetime": "2026-06-11T12:00:05",
        "data": {
            "error_type": error_type,
            "error_message": error_message,
            "error_stack": None,
            "recoverable": True,
            "context": {},
        },
    }


def _session_completion(
    success=True,
    total_steps=5,
    final_result="Task completed successfully.",
    duration_seconds=12.3,
) -> dict:
    """Mirror SessionCompletionFormatter.format() output."""
    return {
        "type": "session_completion",
        "timestamp": 1234567890.0,
        "datetime": "2026-06-11T12:00:06",
        "data": {
            "success": success,
            "total_steps": total_steps,
            "duration_seconds": duration_seconds,
            "error_message": None,
            "metrics": {
                "final_result": final_result,
            },
        },
    }


# ---------------------------------------------------------------------------
# session_start
# ---------------------------------------------------------------------------


def test_normalize_session_start_type():
    ev = normalize(_session_start())
    assert isinstance(ev, SessionStart)


def test_normalize_session_start_fields():
    ev = normalize(_session_start(
        task="do something",
        model_name="claude-sonnet-4",
        agent_id="executor_xyz",
        use_vision=True,
    ))
    assert isinstance(ev, SessionStart)
    assert ev.task == "do something"
    assert ev.model_name == "claude-sonnet-4"
    assert ev.agent_id == "executor_xyz"
    assert ev.use_vision is True


def test_normalize_session_start_preserves_raw():
    raw = _session_start()
    ev = normalize(raw)
    assert ev.raw is raw


# ---------------------------------------------------------------------------
# llm_request — the critical regression test for the wrong-key bug
# ---------------------------------------------------------------------------


def test_normalize_llm_request_type():
    ev = normalize(_llm_request())
    assert isinstance(ev, LLMCall)


def test_normalize_llm_request_cost_estimate_key():
    """cost_estimate (NOT 'cost') must flow through."""
    ev = normalize(_llm_request(cost_estimate=0.001234))
    assert isinstance(ev, LLMCall)
    assert ev.cost_estimate == pytest.approx(0.001234)


def test_normalize_llm_request_token_keys():
    """token_count / prompt_tokens / completion_tokens (NOT 'total_tokens') must flow through."""
    ev = normalize(_llm_request(
        prompt_tokens=500,
        completion_tokens=100,
        token_count=600,
    ))
    assert isinstance(ev, LLMCall)
    assert ev.prompt_tokens == 500
    assert ev.completion_tokens == 100
    assert ev.token_count == 600


def test_normalize_llm_request_all_fields():
    ev = normalize(_llm_request(
        model_name="gpt-4o",
        prompt_tokens=1024,
        completion_tokens=256,
        token_count=1280,
        cost_estimate=0.000512,
        duration_seconds=1.5,
        success=False,
        provider="openai",
    ))
    assert isinstance(ev, LLMCall)
    assert ev.model_name == "gpt-4o"
    assert ev.provider == "openai"
    assert ev.prompt_tokens == 1024
    assert ev.completion_tokens == 256
    assert ev.token_count == 1280
    assert ev.cost_estimate == pytest.approx(0.000512)
    assert ev.duration_seconds == pytest.approx(1.5)
    assert ev.success is False


def test_normalize_llm_request_missing_cost_is_none():
    """When cost_estimate is absent from the feed, LLMCall.cost_estimate is None."""
    d = _llm_request()
    del d["data"]["cost_estimate"]
    ev = normalize(d)
    assert isinstance(ev, LLMCall)
    assert ev.cost_estimate is None


def test_normalize_llm_request_missing_tokens_are_none():
    """When token fields are absent, they are None (not 0)."""
    d = _llm_request()
    del d["data"]["prompt_tokens"]
    del d["data"]["completion_tokens"]
    del d["data"]["token_count"]
    ev = normalize(d)
    assert isinstance(ev, LLMCall)
    assert ev.prompt_tokens is None
    assert ev.completion_tokens is None
    assert ev.token_count is None


# ---------------------------------------------------------------------------
# step
# ---------------------------------------------------------------------------


def test_normalize_step_type():
    ev = normalize(_step())
    assert isinstance(ev, Step)


def test_normalize_step_fields():
    ev = normalize(_step(
        step=7,
        reasoning="Checking the config file.",
        memory="tracking auth",
    ))
    assert isinstance(ev, Step)
    assert ev.step == 7
    assert ev.reasoning == "Checking the config file."
    assert ev.memory == "tracking auth"


def test_normalize_step_actions():
    actions = [
        {"action_type": "read_file", "name": "read_file", "service": "filesystem",
         "params": {"file_path": "foo.py"}},
    ]
    ev = normalize(_step(actions=actions))
    assert isinstance(ev, Step)
    assert len(ev.actions) == 1
    assert ev.actions[0]["name"] == "read_file"


def test_normalize_step_missing_memory():
    """memory absent from context.outputs → empty string."""
    d = _step()
    d["data"]["context"] = {}
    ev = normalize(d)
    assert isinstance(ev, Step)
    assert ev.memory == ""


# ---------------------------------------------------------------------------
# tool_execution
# ---------------------------------------------------------------------------


def test_normalize_tool_execution_type():
    ev = normalize(_tool_execution())
    assert isinstance(ev, ToolExec)


def test_normalize_tool_execution_fields():
    ev = normalize(_tool_execution(
        tool_name="browser",
        action_name="navigate_to",
        success=True,
        duration_seconds=0.5,
        step=4,
    ))
    assert isinstance(ev, ToolExec)
    assert ev.tool_name == "browser"
    assert ev.action_name == "navigate_to"
    assert ev.success is True
    assert ev.duration_seconds == pytest.approx(0.5)
    assert ev.step == 4


def test_normalize_tool_execution_error():
    ev = normalize(_tool_execution(success=False, error="timeout"))
    assert isinstance(ev, ToolExec)
    assert ev.success is False
    assert ev.error == "timeout"


def test_normalize_tool_execution_carries_params_and_preview():
    """A1: ToolExec must surface the args + result preview the feed already carries.

    The ``tool_execution`` feed dict carries ``parameters``, ``result_preview``
    and ``result_truncated`` — these are needed to render a tool transcript by
    default (not just under /verbose). They were previously dropped.
    """
    ev = normalize(_tool_execution())
    assert isinstance(ev, ToolExec)
    assert ev.parameters == {"file_path": "config.py"}
    assert ev.result_preview == "file content..."
    assert ev.result_truncated is False


def test_normalize_tool_execution_missing_optional_fields_default_safe():
    """Older/partial feed dicts (no parameters/preview) normalise without error."""
    raw = {
        "type": "tool_execution",
        "step": 1,
        "data": {"tool_name": "x", "action_name": "y", "success": True},
    }
    ev = normalize(raw)
    assert isinstance(ev, ToolExec)
    assert ev.parameters == {}
    assert ev.result_preview is None
    assert ev.result_truncated is False


# ---------------------------------------------------------------------------
# iteration_complete
# ---------------------------------------------------------------------------


def test_normalize_iteration_complete_type():
    ev = normalize(_iteration_complete())
    assert isinstance(ev, IterationDone)


def test_normalize_iteration_complete_fields():
    ev = normalize(_iteration_complete(
        iteration=5,
        step=5,
        iteration_status="completed",
        is_done=True,
    ))
    assert isinstance(ev, IterationDone)
    assert ev.iteration == 5
    assert ev.iteration_status == "completed"
    assert ev.is_done is True


# ---------------------------------------------------------------------------
# error
# ---------------------------------------------------------------------------


def test_normalize_error_type():
    ev = normalize(_error())
    assert isinstance(ev, ErrorEvent)


def test_normalize_error_fields():
    ev = normalize(_error(
        error_message="network timeout",
        error_type="NetworkError",
        step=3,
    ))
    assert isinstance(ev, ErrorEvent)
    assert ev.error_message == "network timeout"
    assert ev.error_type == "NetworkError"
    assert ev.step == 3


# ---------------------------------------------------------------------------
# session_completion
# ---------------------------------------------------------------------------


def test_normalize_session_completion_type():
    ev = normalize(_session_completion())
    assert isinstance(ev, SessionDone)


def test_normalize_session_completion_fields():
    ev = normalize(_session_completion(
        success=True,
        total_steps=10,
        final_result="All done.",
        duration_seconds=30.0,
    ))
    assert isinstance(ev, SessionDone)
    assert ev.success is True
    assert ev.total_steps == 10
    assert ev.final_result == "All done."
    assert ev.duration_seconds == pytest.approx(30.0)


def test_normalize_session_completion_failed():
    ev = normalize(_session_completion(success=False, final_result=""))
    assert isinstance(ev, SessionDone)
    assert ev.success is False


# ---------------------------------------------------------------------------
# Info / passthrough types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        "status",
        "user_message",
        "queue_status",
        "multi_agent_relationship",
        "multi_agent_relationship_detailed",
        "session_relationship",
        "available_actions",
        "session_paused",
        "session_resumed",
    ],
)
def test_normalize_passthrough_types_become_info(event_type: str):
    d = {"type": event_type, "timestamp": 0.0, "data": {}}
    ev = normalize(d)
    assert isinstance(ev, Info)
    assert ev.type == event_type


def test_normalize_unknown_type_becomes_info_not_crash():
    """Completely unknown types must normalise to Info, never raise."""
    d = {"type": "some_future_event", "timestamp": 0.0, "data": {"foo": "bar"}}
    ev = normalize(d)
    assert isinstance(ev, Info)
    assert ev.type == "some_future_event"


def test_normalize_missing_type_becomes_info():
    """Events without a 'type' key must not crash."""
    ev = normalize({"data": {}})
    assert isinstance(ev, Info)


def test_normalize_empty_dict_becomes_info():
    ev = normalize({})
    assert isinstance(ev, Info)


def test_normalize_dead_type_planner_becomes_info():
    """'planner' was a dead branch in old _feed.py; it becomes Info now."""
    d = {"type": "planner", "data": {"plan": "step 1, step 2"}}
    ev = normalize(d)
    assert isinstance(ev, Info)


def test_normalize_dead_type_evaluation_becomes_info():
    """'evaluation' was a dead branch in old _feed.py; it becomes Info now."""
    d = {"type": "evaluation", "data": {"status": "done", "reason": "looks good"}}
    ev = normalize(d)
    assert isinstance(ev, Info)


def test_normalize_dead_type_action_result_becomes_info():
    d = {"type": "action_result", "data": {"result": "ok"}}
    ev = normalize(d)
    assert isinstance(ev, Info)


def test_normalize_agent_registration_is_typed():
    """agent_registration IS live-emitted (§0 amendment 2) — now a typed event."""
    from cli.ui.events import AgentRegistration

    d = {
        "type": "agent_registration",
        "data": {
            "agent_id": "executor_abc",
            "agent_name": "executor",
            "agent_type": "Agent",
            "model_name": "gemini-2.5-flash",
            "task": "do the thing",
            "session_id": "abc",
        },
    }
    ev = normalize(d)
    assert isinstance(ev, AgentRegistration)
    assert ev.agent_id == "executor_abc"
    assert ev.agent_name == "executor"
    assert ev.model_name == "gemini-2.5-flash"
    assert ev.task == "do the thing"


def test_normalize_agent_end_is_typed():
    """agent_end IS live-emitted (§0 amendment 2) — now a typed event."""
    from cli.ui.events import AgentEnd

    d = {
        "type": "agent_end",
        "data": {
            "agent_id": "executor_abc",
            "steps": 2,
            "max_steps_reached": False,
            "success": True,
            "errors": [],
        },
    }
    ev = normalize(d)
    assert isinstance(ev, AgentEnd)
    assert ev.agent_id == "executor_abc"
    assert ev.steps == 2
    assert ev.success is True
    assert ev.max_steps_reached is False
