"""
AutoV2 Agent Framework

This package provides advanced agent capabilities for autonomous tasks.

Usage:
    from agents.task import pm, get_session_manager

    # Clean a session ID
    clean_id = pm().clean_session_id(session_id)
    
    # Get session manager
    session_mgr = get_session_manager()
    
    # Create a session
    session_id = session_mgr.create_session()
"""

from core.version import __version__  # noqa: F401  (project version SSOT)

from typing import TYPE_CHECKING

# Lazy package (PEP 562): importing any `agents.task.*` leaf (e.g. agents.task.constants,
# the import-light flag/config module) must NOT eager-load the agent session/orchestrator
# stack (which pulls the LLM SDKs). The helpers below resolve on first attribute access so
# `from agents.task import pm, get_session_manager` keeps working unchanged.
# See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (P1b).
_LAZY_ATTRS = {
    "pm": (".path", "pm"),
    "get_safe_singleton": (".path", "get_safe_singleton"),
    "send_telemetry": (".utils", "send_telemetry"),
    "get_session_manager": (".agent.session", "get_session_manager"),
}

if TYPE_CHECKING:  # static analysis / IDEs only — no runtime import
    from agents.task.path import pm, get_safe_singleton
    from agents.task.utils import send_telemetry
    from agents.task.agent.session import get_session_manager


def __getattr__(name: str):
    """PEP 562 lazy resolution; caches into globals() so it fires once per name."""
    if name in _LAZY_ATTRS:
        import importlib
        module_path, attr = _LAZY_ATTRS[name]
        value = getattr(importlib.import_module(module_path, __name__), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_ATTRS))

# Session ID best practices
"""
STANDARD PATTERN FOR SESSION ID HANDLING:

1. Always clean session IDs at the entry point of public methods:
   ```python
   def my_method(self, session_id: str):
       clean_id = pm().clean_session_id(session_id)
       # Use clean_id for all operations
   ```

2. Always use pm().clean_session_id() as the canonical cleaning method
   
3. When extracting session ID from agent_id:
   ```python
   if '_' in agent_id:
       session_id = agent_id.split('_', 1)[1]
       clean_id = pm().clean_session_id(session_id)
   ```
   
4. Never perform string transformations directly on session IDs
"""

from typing import Optional, Dict, Any

def capture_llm_request(
    component: str,
    purpose: str,
    model_name: str,
    duration_seconds: float,
    success: bool,
    token_count: Optional[int] = None,
    session_id: Optional[str] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    cached_tokens: Optional[int] = None,
    agent_id: Optional[str] = None,
    total_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    **kwargs  # Accept any additional kwargs for forward compatibility
) -> str:
    """
    Capture details about an LLM request for telemetry and monitoring.

    This is a thin wrapper around ProductTelemetry.capture_llm_usage to support
    existing tests and code that depends on this function.

    Args:
        component: Component using the LLM (agent, planner, etc.)
        purpose: Purpose of the request (next_action, planning, etc.)
        model_name: Name of the model used
        duration_seconds: Duration of the request in seconds
        success: Whether the request succeeded
        token_count: Total token count (if available)
        session_id: Session ID for tracking
        prompt_tokens: Number of tokens in the prompt (if available)
        completion_tokens: Number of tokens in the completion (if available)
        cached_tokens: Number of cached prompt tokens (for prompt caching cost calculation)
        agent_id: Optional agent ID for explicit tracking
        total_tokens: Total tokens (alternative to token_count)
        temperature: Temperature setting used for the request
        max_tokens: Max tokens setting used for the request
        **kwargs: Additional parameters for forward compatibility

    Returns:
        str: The request_id used for this telemetry entry
    """
    try:
        # Import here to avoid circular imports
        from agents.task.telemetry.service import ProductTelemetry
        
        # Get telemetry service
        telemetry = ProductTelemetry()
        
        # Use total_tokens if provided and token_count is not
        if total_tokens and not token_count:
            token_count = total_tokens
        
        # Prepare parameters dict with additional metadata
        parameters = {}
        if temperature is not None:
            parameters['temperature'] = temperature
        if max_tokens is not None:
            parameters['max_tokens'] = max_tokens
        # Add any other kwargs to parameters
        for key, value in kwargs.items():
            if key not in ['total_tokens']:  # Skip already handled
                parameters[key] = value
        
        # Forward to capture_llm_usage method and return the request_id
        return telemetry.capture_llm_usage(
            component=component,
            purpose=purpose,
            model_name=model_name,
            duration_seconds=duration_seconds,
            success=success,
            token_count=token_count,
            session_id=session_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            agent_id=agent_id,
            parameters=parameters if parameters else None
        )
    except Exception as e:
        # Silently fail for telemetry functions to avoid breaking main code
        return ""
