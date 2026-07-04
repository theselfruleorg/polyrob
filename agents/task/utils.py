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
from typing import Any, Callable, Coroutine, Dict, Optional, ParamSpec, TypeVar, Union, Generic, List
import os
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

def detect_llm_provider(result: Any, model_name: Optional[str] = None) -> str:
    """Detect LLM provider from model name using the model_registry.

    Args:
        result: LLM response object (not used, kept for compatibility)
        model_name: Model name for detection

    Returns:
        Provider name ('openai', 'anthropic', 'gemini', 'deepseek', 'openrouter', 'llama', or 'generic')
    """
    if not model_name:
        return 'generic'
    
    try:
        # Use model_registry as the SINGLE SOURCE OF TRUTH (WS-2.3): the canonical
        # enum→string map lives there, so a new provider is added in one place and
        # GOOGLE→'gemini' can never drift back to 'google' here.
        from modules.llm.model_registry import get_model_config, canonical_provider_name

        model_config = get_model_config(model_name)
        if model_config and model_config.provider:
            return canonical_provider_name(model_config.provider, default='generic')

    except ImportError:
        logger.debug("model_registry not available for provider detection")

    return 'generic'

def extract_token_usage(result: Any, provider: str) -> Dict[str, Optional[int]]:
    """Extract token usage from LLM response using provider-specific paths.

    Args:
        result: LLM response object (could be raw LLM response or structured output)
        provider: Provider name from detect_llm_provider

    Returns:
        Dictionary with token counts (total_tokens, prompt_tokens, completion_tokens, cached_tokens)
    """
    token_usage = {'total_tokens': None, 'prompt_tokens': None, 'completion_tokens': None, 'cached_tokens': None, 'cache_creation_tokens': None}

    try:
        # Handle structured output format: {'parsed': ..., 'raw': <llm_response>}
        actual_result = result
        if isinstance(result, dict) and 'raw' in result:
            # Structured output - extract the raw LLM response
            actual_result = result['raw']

        # Try AIMessage format first (usage_metadata attribute)
        if hasattr(actual_result, 'usage_metadata') and actual_result.usage_metadata:
            usage = actual_result.usage_metadata
            # Handle both dict and object formats (adapters may pass dict or object)
            if isinstance(usage, dict):
                # Dict format (from our adapters)
                token_usage['prompt_tokens'] = (
                    usage.get('input_tokens') or
                    usage.get('prompt_tokens')
                )
                token_usage['completion_tokens'] = (
                    usage.get('output_tokens') or
                    usage.get('completion_tokens')
                )
                token_usage['total_tokens'] = usage.get('total_tokens')
                token_usage['cached_tokens'] = usage.get('cache_read_input_tokens') or usage.get('cached_tokens')
                token_usage['cache_creation_tokens'] = usage.get('cache_creation_input_tokens')
            else:
                # Object format
                token_usage['prompt_tokens'] = (
                    getattr(usage, 'input_tokens', None) or
                    getattr(usage, 'prompt_tokens', None)
                )
                token_usage['completion_tokens'] = (
                    getattr(usage, 'output_tokens', None) or
                    getattr(usage, 'completion_tokens', None)
                )
                token_usage['cached_tokens'] = getattr(usage, 'cache_read_input_tokens', None)
            # Calculate total if not already set
            if token_usage['total_tokens'] is None and token_usage['prompt_tokens'] and token_usage['completion_tokens']:
                token_usage['total_tokens'] = token_usage['prompt_tokens'] + token_usage['completion_tokens']

        # Try response_metadata format
        elif hasattr(actual_result, 'response_metadata'):
            metadata = actual_result.response_metadata
            if 'token_usage' in metadata:
                usage = metadata['token_usage']
                token_usage['prompt_tokens'] = usage.get('prompt_tokens')
                token_usage['completion_tokens'] = usage.get('completion_tokens')
                token_usage['total_tokens'] = usage.get('total_tokens')
                token_usage['cached_tokens'] = usage.get('cached_tokens')

        # Direct attribute access based on known provider patterns
        elif provider == 'openai':
            # OpenAI format: result.usage.{total_tokens, prompt_tokens, completion_tokens}
            if hasattr(actual_result, 'usage') and actual_result.usage:
                usage = actual_result.usage
                token_usage['total_tokens'] = getattr(usage, 'total_tokens', None)
                token_usage['prompt_tokens'] = getattr(usage, 'prompt_tokens', None)
                token_usage['completion_tokens'] = getattr(usage, 'completion_tokens', None)
                token_usage['cached_tokens'] = getattr(usage, 'cached_tokens', None)

        elif provider == 'anthropic':
            # Anthropic format: result.usage.{input_tokens, output_tokens}
            if hasattr(actual_result, 'usage') and actual_result.usage:
                usage = actual_result.usage
                token_usage['prompt_tokens'] = getattr(usage, 'input_tokens', None)
                token_usage['completion_tokens'] = getattr(usage, 'output_tokens', None)
                token_usage['cached_tokens'] = getattr(usage, 'cache_read_input_tokens', None)
            # Sometimes Anthropic puts these directly on result
            elif hasattr(actual_result, 'input_tokens'):
                token_usage['prompt_tokens'] = getattr(actual_result, 'input_tokens', None)
                token_usage['completion_tokens'] = getattr(actual_result, 'output_tokens', None)

        else:
            # Generic fallback - try multiple common patterns
            if hasattr(actual_result, 'usage') and actual_result.usage:
                usage = actual_result.usage
                # Try OpenAI pattern first
                token_usage['total_tokens'] = getattr(usage, 'total_tokens', None)
                token_usage['prompt_tokens'] = getattr(usage, 'prompt_tokens', None)
                token_usage['completion_tokens'] = getattr(usage, 'completion_tokens', None)
                token_usage['cached_tokens'] = getattr(usage, 'cached_tokens', None) or getattr(usage, 'cache_read_input_tokens', None)
                # Try Anthropic pattern if OpenAI didn't work
                if not token_usage['prompt_tokens']:
                    token_usage['prompt_tokens'] = getattr(usage, 'input_tokens', None)
                if not token_usage['completion_tokens']:
                    token_usage['completion_tokens'] = getattr(usage, 'output_tokens', None)

        # Calculate total if we have prompt + completion but no total
        if (token_usage['total_tokens'] is None and
            token_usage['prompt_tokens'] is not None and
            token_usage['completion_tokens'] is not None):
            token_usage['total_tokens'] = token_usage['prompt_tokens'] + token_usage['completion_tokens']

        # Ensure all values are proper integers or None
        for key in token_usage:
            if token_usage[key] is not None:
                try:
                    token_usage[key] = int(token_usage[key])
                except (ValueError, TypeError):
                    token_usage[key] = None

    except Exception as e:
        logging.getLogger('task.utils').debug(f"Error extracting token usage: {e}")

    return token_usage


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
    if os.environ.get('ANONYMIZED_TELEMETRY', 'true').lower() != 'true':
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


# GIF creation utilities have been moved to utils/gif_utils.py
# For backwards compatibility, import them here
from utils.gif_utils import create_gif_with_retry, create_text_only_gif as _create_text_only_gif

# DEPRECATED FUNCTIONS REMOVED - Use modules.llm.llm_factory instead
# These functions were causing conflicts with the main LLM package and only supported OpenAI
# Legacy LLM factory functions have been completely removed
# Use the centralized LLM factory: modules.llm.llm_factory.create_chat_model

# Add the unified file locking implementation after the singleton decorator code

class SafeFileLock:
    """
    Cross-platform file locking implementation with fallback mechanism.
    
    This provides a unified locking mechanism that works across:
    1. Linux/macOS (using filelock package if available)
    2. Windows (using filelock package if available)
    3. Any platform (using a simple lock file as fallback)
    
    Usage:
        with SafeFileLock('/path/to/file.lock'):
            # Perform operations requiring exclusive access
    """
    __slots__ = ('lock_file', 'timeout', 'poll_interval', '_lock', '_locked', '_use_filelock', '_filelock')
    
    # FIXED: Track acquired locks with bounded cache and proper cleanup
    _active_locks = {}  # Maps lock file paths to lock counters
    _REGISTRY_LOCK = threading.RLock()  # Lock for modifying _active_locks
    _last_cleanup = 0  # Track last cleanup time
    _MAX_CACHE_SIZE = 1000  # FIXED: Limit cache size to prevent memory leaks
    _CLEANUP_INTERVAL = 60  # FIX 9: Reduced from 300 to 60 seconds for more aggressive cleanup
    _STALE_LOCK_AGE = 300  # FIX 9: Consider locks stale after 5 minutes (was 3600/1 hour)
    
    def __init__(self, lock_file: str, timeout: float = 60.0, poll_interval: float = 0.1):
        """Initialize the SafeFileLock.

        Args:
            lock_file: Path to the lock file
            timeout: Maximum time to wait for lock acquisition
            poll_interval: Time between lock acquisition attempts
        """
        self.lock_file = os.path.abspath(lock_file)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._lock = None
        self._locked = False
        self._use_filelock = False
        self._filelock = None

        # FIX 9: Check if our specific lock file is stale before trying to acquire
        if os.path.exists(self.lock_file):
            try:
                file_age = time.time() - os.path.getmtime(self.lock_file)
                if file_age > self._STALE_LOCK_AGE:
                    # Try to remove our specific stale lock file
                    try:
                        os.unlink(self.lock_file)
                        logger.debug(f"Removed stale lock on init: {self.lock_file} (age: {file_age:.1f}s)")
                    except (OSError, PermissionError):
                        logger.debug(f"Could not remove stale lock on init: {self.lock_file}")
            except (OSError, FileNotFoundError):
                pass

        # Try to use filelock package if available
        try:
            import filelock
            self._filelock = filelock.FileLock(self.lock_file, timeout=timeout)
            self._use_filelock = True
        except ImportError:
            # Fall back to simple file-based locking
            pass

        # FIXED: Trigger cleanup periodically to prevent memory leaks
        self._maybe_cleanup_stale_locks()
    
    @classmethod
    def _maybe_cleanup_stale_locks(cls):
        """Cleanup stale locks and limit cache size to prevent memory leaks."""
        current_time = time.time()
        
        # Only cleanup every _CLEANUP_INTERVAL seconds
        if current_time - cls._last_cleanup < cls._CLEANUP_INTERVAL:
            return
            
        with cls._REGISTRY_LOCK:
            try:
                # FIXED: Remove locks that are no longer held
                stale_locks = []
                for lock_path, count in list(cls._active_locks.items()):
                    # Remove locks with zero or negative count
                    if count <= 0:
                        stale_locks.append(lock_path)
                        continue
                        
                    # FIX 9: More aggressive stale lock detection and cleanup
                    try:
                        if os.path.exists(lock_path):
                            # Check if lock file is older than _STALE_LOCK_AGE seconds
                            file_age = current_time - os.path.getmtime(lock_path)
                            if file_age > cls._STALE_LOCK_AGE:  # FIX 9: Use configurable timeout
                                # Try to remove stale lock file
                                try:
                                    os.unlink(lock_path)
                                    stale_locks.append(lock_path)
                                    logger.debug(f"Removed stale lock file {lock_path} (age: {file_age:.1f}s)")
                                except (OSError, PermissionError):
                                    # Can't remove, but don't count it as active
                                    logger.debug(f"Could not remove stale lock {lock_path}: permission denied")
                                    pass
                        else:
                            # Lock file doesn't exist, remove from tracking
                            stale_locks.append(lock_path)
                    except (OSError, FileNotFoundError):
                        # File system error, remove from tracking
                        stale_locks.append(lock_path)
                
                # Remove stale locks from tracking
                for lock_path in stale_locks:
                    cls._active_locks.pop(lock_path, None)
                
                # FIXED: Implement bounded cache - if cache is too large, remove oldest entries
                if len(cls._active_locks) > cls._MAX_CACHE_SIZE:
                    # Sort by path and remove oldest (we don't have timestamps, so use lexicographic)
                    sorted_locks = sorted(cls._active_locks.items())
                    locks_to_remove = len(cls._active_locks) - (cls._MAX_CACHE_SIZE // 2)  # Remove half
                    
                    for i in range(locks_to_remove):
                        if i < len(sorted_locks):
                            lock_path, count = sorted_locks[i]
                            # Only remove if not currently held
                            if count <= 0:
                                cls._active_locks.pop(lock_path, None)
                
                # Update last cleanup time
                cls._last_cleanup = current_time
                
                if stale_locks:
                    # Use basic logging to avoid circular imports
                    print(f"SafeFileLock: Cleaned up {len(stale_locks)} stale locks")
                    
            except Exception as e:
                # Log cleanup errors but don't raise
                print(f"SafeFileLock cleanup error: {e}")
    
    def __enter__(self):
        """Acquire the lock."""
        if self._use_filelock and self._filelock:
            try:
                self._filelock.acquire(timeout=self.timeout)
                self._locked = True
                
                # Track in registry
                with self._REGISTRY_LOCK:
                    self._active_locks[self.lock_file] = self._active_locks.get(self.lock_file, 0) + 1
                
                return self
            except Exception as e:
                # Fall back to manual locking if filelock fails
                self._use_filelock = False
        
        # Manual file-based locking. Attempt acquisition at least once regardless of
        # timeout (do-while), so a FREE lock is acquired even with timeout=0; only a
        # CONTENDED lock fails fast at timeout=0. (The old `while elapsed < timeout`
        # never ran the body at timeout=0, so a free lock could never be acquired —
        # which silently disabled the goal dispatcher / cron tick on machines without
        # the `filelock` package, where this fallback path is taken.)
        start_time = time.time()

        while True:
            try:
                # FIXED: Use exclusive create mode to prevent race conditions
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                try:
                    # Write PID to lock file for debugging
                    os.write(fd, f"{os.getpid()}\n".encode())
                finally:
                    os.close(fd)

                self._locked = True

                # Track in registry
                with self._REGISTRY_LOCK:
                    self._active_locks[self.lock_file] = self._active_locks.get(self.lock_file, 0) + 1

                return self

            except FileExistsError:
                # Lock file already exists; retry until the timeout budget is spent.
                if time.time() - start_time >= self.timeout:
                    break
                time.sleep(self.poll_interval)
                continue
            except OSError as e:
                # Other file system error
                raise RuntimeError(f"Failed to acquire lock {self.lock_file}: {e}")

        # Timeout reached
        raise TimeoutError(f"Failed to acquire lock {self.lock_file} within {self.timeout} seconds")
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Release the lock."""
        if not self._locked:
            return
        
        try:
            if self._use_filelock and self._filelock:
                self._filelock.release()
            else:
                # Manual lock release
                try:
                    # FIXED: Verify we still own the lock before removing
                    if os.path.exists(self.lock_file):
                        # Check if we own this lock by reading PID
                        try:
                            with open(self.lock_file, 'r') as f:
                                lock_pid = f.read().strip()
                                if lock_pid == str(os.getpid()):
                                    os.unlink(self.lock_file)
                                else:
                                    # Lock was taken by another process, don't remove
                                    pass
                        except (OSError, ValueError):
                            # Can't read lock file, try to remove anyway
                            try:
                                os.unlink(self.lock_file)
                            except (OSError, FileNotFoundError):
                                pass
                except (OSError, FileNotFoundError):
                    # Lock file already removed or can't be removed
                    pass
            
            # Update registry
            with self._REGISTRY_LOCK:
                if self.lock_file in self._active_locks:
                    self._active_locks[self.lock_file] -= 1
                    # FIXED: Clean up zero-count entries immediately
                    if self._active_locks[self.lock_file] <= 0:
                        self._active_locks.pop(self.lock_file, None)
            
        finally:
            self._locked = False


def get_safe_file_lock(lock_file: str, timeout: float = 60.0) -> Any:
    """Get a SafeFileLock instance.
    
    Args:
        lock_file: Path to the lock file
        timeout: Timeout for lock acquisition
        
    Returns:
        SafeFileLock instance
    """
    return SafeFileLock(lock_file, timeout)

