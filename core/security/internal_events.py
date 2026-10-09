"""Authentication for the optional local telemetry fast-push channel."""
import hashlib
import hmac
import os


def emit_token() -> str:
    secret = os.getenv("API_AUTH_TOKEN") or os.getenv("WEBGATE_JWT_SECRET") or ""
    if not secret:
        return ""
    return hmac.new(secret.encode(), b"polyrob:internal-emit:v1", hashlib.sha256).hexdigest()


#: The feed event types the fast-push may carry (WEB-10). The token is derived
#: from a secret several local units load, so it proves "a local polyrob
#: process", not "the owner": an agent-side process may push agent, tool, run
#: and status events, never a line drawn as a PERSON's words. The owner's own
#: lines (``user_message*``) and the console's verb answers (``command_reply``)
#: reach the live view only through the feed watcher / the console itself.
#: An unknown type is refused (the file write stays the source of truth, so a
#: refused push costs latency, never the event).
INTERNAL_EMIT_TYPES = frozenset({
    # formatter outputs (agents/task/telemetry/formatters.py)
    "step", "llm_request", "session_start", "available_actions",
    "tool_execution", "tool_result", "error", "session_completion",
    "queue_status", "session_paused", "session_resumed", "status",
    "iteration_complete",
    # 019 run-state events
    "tool_started", "llm_started", "awaiting_approval", "approval_resolved",
    "compaction_started", "compaction_finished", "retry_wait",
    "subagent_started", "subagent_finished", "delegation_dispatched",
    "delegation_completed",
    # generic-formatter telemetry names (agents/task/telemetry/views.py)
    "agent_step", "agent_run", "agent_end", "interface_event",
    "multi_agent_relationship", "multi_agent_relationship_detailed",
    "screenshot_saved", "agent_registration", "human_approval_requested",
    "human_approval_decision", "todo_status", "streaming_output",
    "agent_question", "provider_failure", "provider_fallback_success",
    # the agent's own words / turn close
    "agent_message", "final_message", "result", "task_complete",
})


def internal_emit_allowed(event_type) -> bool:
    """Whether the fast-push may carry an event of *event_type* (WEB-10)."""
    return isinstance(event_type, str) and event_type in INTERNAL_EMIT_TYPES
