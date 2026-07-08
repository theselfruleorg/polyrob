"""Typed feed-event model for the POLYROB CLI renderer (Phase 1).

Normalises the 14 real feed dicts emitted by
``agents/task/telemetry/formatters.py`` into a small set of typed
``RenderEvent`` dataclasses.  The field names are taken DIRECTLY from the
formatter source — this file is the authoritative mapping so that wrong-key
bugs like the ``cost``/``cost_estimate`` or ``total_tokens``/``token_count``
mis-reads in the old ``_feed.py`` cannot silently recur.

Key mapping (formatters.py → RenderEvent field):
  llm_request
    data.model_name       → LLMCall.model_name
    data.provider         → LLMCall.provider
    data.prompt_tokens    → LLMCall.prompt_tokens
    data.completion_tokens→ LLMCall.completion_tokens
    data.token_count      → LLMCall.token_count
    data.cost_estimate    → LLMCall.cost_estimate   (NOT "cost")
    data.duration_seconds → LLMCall.duration_seconds
    data.success          → LLMCall.success

  step
    data.reasoning        → Step.reasoning
    data.context.outputs.memory → Step.memory
    data.actions          → Step.actions

  session_start
    data.task             → SessionStart.task
    data.model_name       → SessionStart.model_name
    data.agent_id         → SessionStart.agent_id
    data.use_vision       → SessionStart.use_vision

  tool_execution
    data.tool_name        → ToolExec.tool_name
    data.action_name      → ToolExec.action_name
    data.success          → ToolExec.success
    data.duration_seconds → ToolExec.duration_seconds
    data.error            → ToolExec.error

  iteration_complete
    data.iteration        → IterationDone.iteration
    data.iteration_status → IterationDone.iteration_status
    data.is_done          → IterationDone.is_done

  error
    data.error_message    → ErrorEvent.error_message
    data.error_type       → ErrorEvent.error_type

  session_completion
    data.success          → SessionDone.success
    data.total_steps      → SessionDone.total_steps
    data.metrics.final_result → SessionDone.final_result
    data.error_message    → SessionDone.error_message

  agent_registration  (live-emitted; §0 amendment 2)
    data.agent_id / agent_name / agent_type / model_name / task → AgentRegistration

  agent_end           (live-emitted; §0 amendment 2)
    data.agent_id / steps / max_steps_reached / success / errors → AgentEnd

All other types (status / user_message / queue_status /
multi_agent_relationship / multi_agent_relationship_detailed /
session_relationship / available_actions / session_paused /
session_resumed) become ``Info``.  Unknown types also become ``Info``
(never crash).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SessionStart:
    """Emitted when the agent session begins."""

    type: str = "session_start"
    task: str = ""
    model_name: str = ""
    agent_id: str = ""
    use_vision: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    """One agent step: reasoning + memory + actions."""

    type: str = "step"
    step: int = 0
    reasoning: str = ""
    memory: str = ""
    actions: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMCall:
    """Result of an LLM API call (real field names from LLMRequestFormatter)."""

    type: str = "llm_request"
    model_name: str = ""
    provider: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    token_count: Optional[int] = None
    cost_estimate: Optional[float] = None
    duration_seconds: float = 0.0
    success: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolExec:
    """Result of a single tool execution (real field names from ToolExecutionFormatter).

    ``parameters``/``result_preview``/``result_truncated`` are carried so the CLI
    can render a tool transcript (call args + result preview) BY DEFAULT, not just
    under ``/verbose``. The feed dict has always carried them; they were previously
    dropped on the floor here.
    """

    type: str = "tool_execution"
    step: int = 0
    tool_name: str = ""
    action_name: str = ""
    success: bool = True
    duration_seconds: float = 0.0
    error: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    result_preview: Optional[str] = None
    result_truncated: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IterationDone:
    """Iteration boundary marker (IterationCompleteFormatter)."""

    type: str = "iteration_complete"
    iteration: int = 0
    step: int = 0
    iteration_status: str = ""
    is_done: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ErrorEvent:
    """An agent error (ErrorFormatter)."""

    type: str = "error"
    step: int = 0
    error_message: str = ""
    error_type: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionDone:
    """Session completion (SessionCompletionFormatter)."""

    type: str = "session_completion"
    success: bool = True
    total_steps: int = 0
    final_result: str = ""
    error_message: str = ""
    duration_seconds: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRegistration:
    """An agent (main or sub-agent) was registered (AgentRegistrationFormatter).

    Real shape (captured live):
        data.agent_id, data.agent_name, data.agent_type, data.model_name,
        data.task, data.session_id
    Used for sub-agent grouping: the first registration is the main agent;
    subsequent registrations with different agent_ids are sub-agents.
    """

    type: str = "agent_registration"
    agent_id: str = ""
    agent_name: str = ""
    agent_type: str = ""
    model_name: str = ""
    task: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentEnd:
    """An agent finished its run (AgentEndFormatter).

    Real shape (captured live):
        data.agent_id, data.steps, data.max_steps_reached, data.success,
        data.errors[]
    """

    type: str = "agent_end"
    agent_id: str = ""
    steps: int = 0
    max_steps_reached: bool = False
    success: bool = True
    errors: List[Any] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Info:
    """Passthrough wrapper for informational / status events.

    Covers: status, user_message, queue_status, multi_agent_relationship,
    multi_agent_relationship_detailed, session_relationship,
    available_actions, session_paused, session_resumed, and any unknown types.
    """

    type: str = "info"
    content: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


from cli.ui.event_registry import RegisteredEvent  # noqa: E402  (no top-level cli deps)
import cli.ui.event_specs  # noqa: E402,F401  — registers built-in extension events (T4-02)

# Union alias for type annotations
RenderEvent = (
    SessionStart
    | Step
    | LLMCall
    | ToolExec
    | IterationDone
    | ErrorEvent
    | SessionDone
    | AgentRegistration
    | AgentEnd
    | RegisteredEvent
    | Info
)

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

#: Feed types that map to ``Info`` (pass-through, no rich extraction needed).
_INFO_TYPES = frozenset(
    {
        "status",
        "user_message",
        "queue_status",
        "multi_agent_relationship",
        "multi_agent_relationship_detailed",
        "session_relationship",
        "available_actions",
        "session_paused",
        "session_resumed",
    }
)


def normalize(feed_dict: Dict[str, Any]) -> RenderEvent:
    """Normalise a raw feed dict into a typed ``RenderEvent``.

    Never raises; unknown types become ``Info``.

    Args:
        feed_dict: A sanitized update dict as emitted by
            ``ProductTelemetry._on_feed_entry``.

    Returns:
        A typed ``RenderEvent`` dataclass.
    """
    try:
        return _normalize_inner(feed_dict)
    except Exception as exc:  # pragma: no cover
        logger.debug("events.normalize error for %r: %s", feed_dict.get("type"), exc)
        return Info(
            type=feed_dict.get("type", "unknown"),
            raw=feed_dict,
        )


def _normalize_inner(feed_dict: Dict[str, Any]) -> RenderEvent:
    event_type = feed_dict.get("type", "unknown")
    data = feed_dict.get("data", {}) or {}

    # Extension seam (D2): a registered spec wins over the built-in fallback, so a
    # new core event type surfaces without editing this function.
    from cli.ui.event_registry import get_spec
    spec = get_spec(event_type)
    if spec is not None:
        return spec.parse(feed_dict)

    if event_type == "session_start":
        return SessionStart(
            task=data.get("task", ""),
            model_name=data.get("model_name", ""),
            agent_id=data.get("agent_id", ""),
            use_vision=bool(data.get("use_vision", False)),
            raw=feed_dict,
        )

    if event_type == "step":
        # memory lives inside data.context.outputs.memory
        ctx = data.get("context", {}) or {}
        outputs = ctx.get("outputs", {}) or {}
        memory = outputs.get("memory", "")
        return Step(
            step=int(feed_dict.get("step", data.get("step", data.get("iteration", 0)))),
            reasoning=data.get("reasoning", ""),
            memory=memory,
            actions=list(data.get("actions", [])),
            raw=feed_dict,
        )

    if event_type == "llm_request":
        cost_raw = data.get("cost_estimate")
        cost = float(cost_raw) if cost_raw is not None else None
        return LLMCall(
            model_name=data.get("model_name", ""),
            provider=data.get("provider", ""),
            prompt_tokens=_int_or_none(data.get("prompt_tokens")),
            completion_tokens=_int_or_none(data.get("completion_tokens")),
            token_count=_int_or_none(data.get("token_count")),
            cost_estimate=cost,
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            success=bool(data.get("success", True)),
            raw=feed_dict,
        )

    if event_type == "tool_execution":
        raw_params = data.get("parameters")
        params = raw_params if isinstance(raw_params, dict) else {}
        preview = data.get("result_preview")
        return ToolExec(
            step=int(feed_dict.get("step", 0)),
            tool_name=data.get("tool_name", ""),
            action_name=data.get("action_name", ""),
            success=bool(data.get("success", True)),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            error=data.get("error"),
            parameters=params,
            result_preview=str(preview) if preview is not None else None,
            result_truncated=bool(data.get("result_truncated", False)),
            raw=feed_dict,
        )

    if event_type == "iteration_complete":
        return IterationDone(
            iteration=int(data.get("iteration", 0)),
            step=int(data.get("step", 0)),
            iteration_status=data.get("iteration_status", ""),
            is_done=bool(data.get("is_done", False)),
            raw=feed_dict,
        )

    if event_type == "error":
        return ErrorEvent(
            step=int(feed_dict.get("step", 0)),
            error_message=data.get("error_message", ""),
            error_type=data.get("error_type", ""),
            raw=feed_dict,
        )

    if event_type == "session_completion":
        metrics = data.get("metrics", {}) or {}
        return SessionDone(
            success=bool(data.get("success", True)),
            total_steps=int(data.get("total_steps", 0)),
            final_result=str(metrics.get("final_result", "") or ""),
            error_message=str(data.get("error_message", "") or ""),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            raw=feed_dict,
        )

    if event_type == "agent_registration":
        return AgentRegistration(
            agent_id=data.get("agent_id", ""),
            agent_name=data.get("agent_name", ""),
            agent_type=data.get("agent_type", ""),
            model_name=data.get("model_name", ""),
            task=data.get("task", ""),
            raw=feed_dict,
        )

    if event_type == "agent_end":
        return AgentEnd(
            agent_id=data.get("agent_id", ""),
            steps=int(data.get("steps", 0)),
            max_steps_reached=bool(data.get("max_steps_reached", False)),
            success=bool(data.get("success", True)),
            errors=list(data.get("errors", []) or []),
            raw=feed_dict,
        )

    if event_type in _INFO_TYPES:
        content = data.get("content", data.get("message", data.get("text", "")))
        return Info(
            type=event_type,
            content=str(content) if content else "",
            raw=feed_dict,
        )

    # Unknown type — log at debug, never crash.
    logger.debug("events.normalize: unknown feed type %r — mapping to Info", event_type)
    content = data.get("content", data.get("message", ""))
    return Info(
        type=event_type,
        content=str(content) if content else "",
        raw=feed_dict,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
