"""
Utility functions for the task package.

Note: All session directory and path management is now handled through
the centralized path management system in agents.task.path:
    
    from agents.task.path import pm
    
    # Examples:
    clean_id = pm().clean_session_id(session_id)
    workspace_dir = pm().get_workspace_dir(session_id)
    file_path = pm().create_file_path(session_id, "workspace", "output.json")
"""

import logging
import time
import json
from typing import Any, Callable, Dict, Optional, ParamSpec, TypeVar
import os
from core.env import bool_env
from pathlib import Path
import threading
import warnings

logger = logging.getLogger(__name__)

def extract_session_from_agent_id(agent_id: str) -> Optional[str]:
    """Extract session ID from agent_id format: name_sessionid

    This utility consolidates duplicate logic found in 5+ places across the codebase.
    Agent IDs follow the format "agent_name_session_id" where the session ID is
    everything after the first underscore.

    Args:
        agent_id: Agent identifier in format "name_sessionid"

    Returns:
        Cleaned session ID or None if agent_id is invalid

    Examples:
        >>> extract_session_from_agent_id("agent_abc123")
        "abc123"
        >>> extract_session_from_agent_id("task_user_session_xyz")
        "user_session_xyz"
        >>> extract_session_from_agent_id("invalid")
        None
    """
    from agents.task.path import pm

    if not agent_id or '_' not in agent_id:
        return None

    # Split only on first underscore to preserve underscores in session_id
    session_id = agent_id.split('_', 1)[1]
    return pm().clean_session_id(session_id)

def fix_schema_for_provider(schema: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """Enforce additionalProperties: false recursively for LLM provider compatibility.

    Both OpenAI and Anthropic require all object-typed nodes to carry
    ``additionalProperties: false``; the only exception is when it is explicitly
    set to ``True`` (indicating a ``Dict[str, Any]`` that must accept arbitrary keys,
    e.g. MCP tool arguments).  All other providers currently get the same treatment.

    Mutates *schema* in place and also returns it (matches the prior contract of
    fix_openai_schema / fix_anthropic_schema so existing callers are unaffected).

    Args:
        schema: JSON schema fragment to fix (mutated in place).
        provider: Provider name string (e.g. "openai", "anthropic").  Currently
            unused because both providers need the same normalisation, but kept for
            future per-provider branching without changing the call sites.

    Returns:
        The same schema dict, normalised.
    """
    if isinstance(schema, dict):
        # Enforce additionalProperties: false on object-typed nodes, UNLESS the
        # caller explicitly opted in to True (Dict[str, Any] / open MCP args).
        if schema.get('type') == 'object':
            if schema.get('additionalProperties') is not True:
                schema['additionalProperties'] = False

        # Recurse into all nested schema locations.
        for key, value in schema.items():
            if key == 'properties' and isinstance(value, dict):
                for prop_name, prop_schema in value.items():
                    schema['properties'][prop_name] = fix_schema_for_provider(prop_schema, provider)
            elif key == 'items' and isinstance(value, dict):
                schema['items'] = fix_schema_for_provider(value, provider)
            elif key == 'definitions' and isinstance(value, dict):
                for def_name, def_schema in value.items():
                    schema['definitions'][def_name] = fix_schema_for_provider(def_schema, provider)
            elif key == '$defs' and isinstance(value, dict):
                for def_name, def_schema in value.items():
                    schema['$defs'][def_name] = fix_schema_for_provider(def_schema, provider)
            elif key == 'anyOf' and isinstance(value, list):
                schema['anyOf'] = [fix_schema_for_provider(s, provider) for s in value]
            elif key == 'oneOf' and isinstance(value, list):
                schema['oneOf'] = [fix_schema_for_provider(s, provider) for s in value]
            elif key == 'allOf' and isinstance(value, list):
                schema['allOf'] = [fix_schema_for_provider(s, provider) for s in value]
            elif isinstance(value, dict):
                schema[key] = fix_schema_for_provider(value, provider)

    return schema


def fix_openai_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Backward-compat wrapper — delegates to fix_schema_for_provider."""
    return fix_schema_for_provider(schema, "openai")


def fix_anthropic_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Backward-compat wrapper — delegates to fix_schema_for_provider."""
    return fix_schema_for_provider(schema, "anthropic")

from modules.llm.usage_extract import (  # noqa: F401  (re-exported for this tier's callers; S5 relocation)
    detect_llm_provider,
    extract_token_usage,
    resolve_serving_provider,
)


# Define generic type variables for return type and parameters
R = TypeVar('R')
P = ParamSpec('P')
T = TypeVar('T')


def handle_with_fallback(
    primary_func: Callable[[], T], 
    fallback_func: Callable[[], T],
    logger: logging.Logger,
    error_msg: str
) -> T:
    """Standard error handling with fallback.
    
    Args:
        primary_func: Primary function to try first
        fallback_func: Fallback function to use if primary fails
        logger: Logger to use for error messages
        error_msg: Message to log on error
        
    Returns:
        Result from either primary or fallback function
    """
    try:
        return primary_func()
    except Exception as e:
        logger.warning(f"{error_msg}: {e}")
        return fallback_func()

def safe_operation(
    func: Callable[[], T],
    logger: logging.Logger,
    error_msg: str,
    default_value: Optional[T] = None
) -> Optional[T]:
    """Execute operation safely, returning default value on failure.

    Args:
        func: Function to execute
        logger: Logger to use for error messages
        error_msg: Message to log on error
        default_value: Value to return if function fails

    Returns:
        Result from function or default value on error
    """
    try:
        return func()
    except Exception as e:
        logger.error(f"{error_msg}: {e}")
        return default_value


# Timing decorators have been moved to utils/time_utils.py
# Import from there for backwards compatibility
from utils.time_utils import time_execution_sync, time_execution_async


def log_llm_request(component: str, purpose: Optional[str] = None) -> Callable:
    """
    Decorator to log and track LLM requests with telemetry.
    
    This decorator provides consistent logging and telemetry capture for LLM operations.
    Used primarily in tests and theatre scripts for tracking.
    
    Args:
        component: Component name making the request
        purpose: Purpose of the request (optional)
    
    Returns:
        Decorated function with logging and telemetry
    """
    import asyncio
    import time
    import logging
    from functools import wraps
    
    logger = logging.getLogger(__name__)
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            
            # Extract LLM info
            llm = kwargs.get('llm') or (args[1] if len(args) > 1 else None)
            agent = args[0] if args else None
            
            model_name = getattr(llm, 'model_name', 'unknown') if llm else 'unknown'
            
            # Log the request
            purpose_str = f"/{purpose}" if purpose else ""
            logger.info(f"🔄 LLM REQUEST [{component}{purpose_str}] - Model: {model_name}")
            
            try:
                # Execute the function
                result = await func(*args, **kwargs)
                
                # Extract token usage if available
                if result and isinstance(result, dict):
                    raw = result.get('raw')
                    if raw and hasattr(raw, 'usage'):
                        usage = raw.usage
                        if usage:
                            prompt_tokens = getattr(usage, 'prompt_tokens', 0)
                            completion_tokens = getattr(usage, 'completion_tokens', 0)
                            total_tokens = getattr(usage, 'total_tokens', 0)
                            
                            duration = time.time() - start_time
                            logger.info(f"Token usage - Prompt: {prompt_tokens}, Completion: {completion_tokens}, Total: {total_tokens}")
                            logger.info(f"Completed in {duration:.2f}s")
                            
                            # Capture telemetry
                            try:
                                from agents.task import capture_llm_request
                                session_id = getattr(agent, 'agent_id', None) if agent else None
                                
                                capture_llm_request(
                                    component=component,
                                    purpose=purpose,
                                    model_name=model_name,
                                    duration_seconds=duration,
                                    success=True,
                                    prompt_tokens=prompt_tokens,
                                    completion_tokens=completion_tokens,
                                    total_tokens=total_tokens,
                                    session_id=session_id,
                                    temperature=getattr(llm, 'temperature', None) if llm else None,
                                    max_tokens=getattr(llm, 'max_tokens', None) if llm else None
                                )
                            except Exception as e:
                                logger.debug(f"Failed to capture telemetry: {e}")
                
                return result
                
            except Exception as e:
                duration = time.time() - start_time
                logger.error(f"LLM request failed after {duration:.2f}s: {e}")
                
                # Capture telemetry for failure
                try:
                    from agents.task import capture_llm_request
                    session_id = getattr(agent, 'agent_id', None) if agent else None
                    
                    capture_llm_request(
                        component=component,
                        purpose=purpose,
                        model_name=model_name,
                        duration_seconds=duration,
                        success=False,
                        session_id=session_id
                    )
                except Exception:
                    pass
                    
                raise
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            # For sync functions, just pass through (tests mostly use async)
            return func(*args, **kwargs)
        
        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator


# get_safe_singleton removed - use get_safe_singleton from agents.task.path instead


def save_registry_items(items: Dict[str, Any], directory: Path, item_type: str = "item") -> None:
    """
    Save registry items (profiles, scenarios, etc.) to disk.
    
    Args:
        items: Dictionary of items to save (id -> model)
        directory: Directory to save items to
        item_type: Type of item for logging messages
    """
    import json
    import logging
    logger = logging.getLogger(__name__)
    
    for item_id, item in items.items():
        item_file = directory / f"{item_id}.json"
        try:
            with open(item_file, 'w') as f:
                # Handle both Pydantic models and dicts
                data = item.model_dump() if hasattr(item, 'model_dump') else item
                json.dump(data, f, indent=2)
            logger.info(f"Saved default {item_type} '{item_id}' to {item_file}")
        except Exception as e:
            logger.error(f"Failed to save {item_type} '{item_id}': {e}")

def send_telemetry(event: Any, agent_id: Optional[str] = None, session_id: Optional[str] = None, user_id: Optional[str] = None) -> bool:
    """
    Send telemetry with consistent session/agent ID handling.
    
    Args:
        event: The telemetry event to send
        agent_id: Optional agent ID for tracking
        session_id: Optional session ID for tracking
        user_id: Optional user ID for multi-tenant tracking
        
    Returns:
        True if telemetry was sent successfully, False otherwise
    """
    # Don't send telemetry if disabled in environment
    if not bool_env('ANONYMIZED_TELEMETRY', False):  # opt-in; ProductTelemetry gates too
        return False
    
    try:
        # Use the get_telemetry function instead of directly importing ProductTelemetry
        from agents.task.telemetry import get_telemetry
        # Import path manager for clean_session_id
        from agents.task.path import pm
        
        # Try to get correct ID to use for tracking
        tracking_id = None
        
        # Use session_id as first choice if provided
        if session_id:
            tracking_id = pm().clean_session_id(session_id)
        # Use agent_id as fallback, extract session ID from it if possible
        elif agent_id:
            tracking_id = extract_session_from_agent_id(agent_id) or pm().clean_session_id(agent_id)
        
        # Send telemetry with proper tracking ID. NOTE: ProductTelemetry.capture()
        # takes only (event, session_id) — telemetry is tenant-scoped by the
        # session's on-disk path, not a user_id arg. Passing user_id= raised a
        # swallowed TypeError, so this always returned False (audit 2026-07-04).
        telemetry = get_telemetry()
        telemetry.capture(event, session_id=tracking_id)
        return True
    except Exception as e:
        # Silent fail for telemetry to avoid affecting main operation
        logger.debug(f"Failed to send telemetry: {e}")
        return False


# DEPRECATED FUNCTIONS REMOVED - Use modules.llm.llm_factory instead
# These functions were causing conflicts with the main LLM package and only supported OpenAI
# Legacy LLM factory functions have been completely removed
# Use the centralized LLM factory: modules.llm.llm_factory.create_chat_model
