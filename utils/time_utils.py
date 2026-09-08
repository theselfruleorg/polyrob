from datetime import datetime, timezone
import logging
import time
import sys
from typing import Any, Callable, TypeVar, Coroutine
import asyncio
import functools

logger = logging.getLogger(__name__)

T = TypeVar('T')
# ParamSpec was introduced in Python 3.10, so we need to handle older versions
if sys.version_info >= (3, 10):
    from typing import ParamSpec
    P = ParamSpec('P')
    USING_PARAMSPEC = True
else:
    # For older Python versions, use TypeVar as a simpler substitute
    P = TypeVar('P')
    USING_PARAMSPEC = False

def get_current_timestamp() -> float:
    """Get current UTC timestamp in seconds since epoch.
    
    Returns:
        Float timestamp in seconds since epoch
    """
    return datetime.now(timezone.utc).timestamp()

# Use simpler type signatures for older Python versions to prevent errors
if USING_PARAMSPEC:
    def time_execution_sync(name: str = '') -> Callable[[Callable[P, T]], Callable[P, T]]:
        """Decorator to time synchronous function execution."""
        def decorator(func: Callable[P, T]) -> Callable[P, T]:
            @functools.wraps(func)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                start = time.time()
                result = func(*args, **kwargs)
                end = time.time()
                logger.debug("%s took %.2f seconds", name, end - start)
                return result
            return wrapper
        return decorator

    def time_execution_async(name: str = '') -> Callable[[Callable[P, Coroutine[Any, Any, T]]], Callable[P, Coroutine[Any, Any, T]]]:
        """Decorator to time asynchronous function execution."""
        def decorator(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Coroutine[Any, Any, T]]:
            @functools.wraps(func)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                start = time.time()
                result = await func(*args, **kwargs)
                end = time.time()
                logger.debug("%s took %.2f seconds", name, end - start)
                return result
            return wrapper
        return decorator
else:
    # Simpler versions for older Python
    def time_execution_sync(name: str = ''):
        """Decorator to time synchronous function execution."""
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                start = time.time()
                result = func(*args, **kwargs)
                end = time.time()
                logger.debug("%s took %.2f seconds", name, end - start)
                return result
            return wrapper
        return decorator

    def time_execution_async(name: str = ''):
        """Decorator to time asynchronous function execution."""
        def decorator(func):
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                start = time.time()
                result = await func(*args, **kwargs)
                end = time.time()
                logger.debug("%s took %.2f seconds", name, end - start)
                return result
            return wrapper
        return decorator

def parse_date_to_timestamp(date_string: str) -> int:
    """Convert various date formats to Unix timestamp.

    Supports:
    - ISO 8601: "2025-11-02T00:00:00Z"
    - Date only: "2025-11-02" (assumes 00:00:00 UTC)
    - With time: "2025-11-02 14:30:00"

    Returns:
        Unix timestamp (seconds since epoch)

    Raises:
        ValueError: If date string cannot be parsed
    """
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",      # ISO 8601 with Z
        "%Y-%m-%dT%H:%M:%S.%fZ",   # ISO 8601 with milliseconds
        "%Y-%m-%dT%H:%M:%S",       # ISO 8601 without Z
        "%Y-%m-%d %H:%M:%S",       # Standard datetime
        "%Y-%m-%d",                # Date only
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_string, fmt)
            # If no timezone info, assume UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue

    raise ValueError(f"Unable to parse date string: {date_string}")

def timestamp_to_date(timestamp: int) -> str:
    """Convert Unix timestamp to ISO 8601 date string.

    Args:
        timestamp: Unix timestamp in seconds

    Returns:
        ISO 8601 formatted date string
    """
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")