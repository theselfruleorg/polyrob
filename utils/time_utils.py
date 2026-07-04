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

def parse_timestamp_to_float(ts_value) -> float:
    """Parse a timestamp value to float.
    
    Args:
        ts_value: Timestamp value to parse. Can be:
            - float/int: returned as float
            - str: parsed as float or datetime
            - datetime: converted to timestamp
            - dict/object with timestamp attribute: extracts timestamp
            
    Returns:
        Float timestamp
        
    Raises:
        ValueError if timestamp cannot be parsed
    """
    try:
        # Handle None
        if ts_value is None:
            return 0.0
            
        # Handle numeric types
        if isinstance(ts_value, (int, float)):
            return float(ts_value)
            
        # Handle datetime
        if isinstance(ts_value, datetime):
            return ts_value.timestamp()
            
        # Handle string
        if isinstance(ts_value, str):
            try:
                return float(ts_value)
            except ValueError:
                # Try parsing as datetime
                try:
                    dt = datetime.fromisoformat(ts_value.replace('Z', '+00:00'))
                    return dt.timestamp()
                except ValueError:
                    pass
                    
        # Handle BotConfig or similar objects
        if hasattr(ts_value, '__dict__'):
            # Try to find a timestamp-like attribute
            for attr in ['timestamp', 'created_at', 'time', 'date']:
                if hasattr(ts_value, attr):
                    val = getattr(ts_value, attr)
                    if val is not None:
                        try:
                            return parse_timestamp_to_float(val)
                        except:
                            continue
            
            # If no timestamp found in attributes, try dictionary values
            if hasattr(ts_value, '__dict__'):
                for key, val in ts_value.__dict__.items():
                    if any(time_key in key.lower() for time_key in ['time', 'date', 'timestamp']):
                        try:
                            return parse_timestamp_to_float(val)
                        except:
                            continue
                            
        # Handle dict-like objects
        if hasattr(ts_value, 'get'):
            # Try common timestamp keys
            for key in ['timestamp', 'created_at', 'time', 'date']:
                if ts_value.get(key) is not None:
                    try:
                        return parse_timestamp_to_float(ts_value.get(key))
                    except:
                        continue
                        
        raise ValueError(f"Unable to parse timestamp of type {type(ts_value)}")
        
    except Exception as e:
        logging.error(f"Error parsing timestamp {ts_value}: {str(e)}")
        return 0.0  # Return epoch on error 

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