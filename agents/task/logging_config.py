"""
Task Agent Logging Configuration

Provides session-aware logging for the task agent system.
Integrates with core.logging when available, with automatic fallback.
"""

import logging
import os
from typing import Optional
from pathlib import Path

# Try to import core logging if available
try:
    from core.logging import get_component_logger
    CORE_LOGGING_AVAILABLE = True
except ImportError:
    CORE_LOGGING_AVAILABLE = False

# Cache for created loggers
_LOGGER_CACHE = {}

def get_task_logger(component_name: str, session_id: Optional[str] = None) -> logging.Logger:
    """
    Get a logger for task agent components.

    Integrates with core logging system when available, with automatic fallback
    to basic logging if core system is not initialized.

    Args:
        component_name: Component name (e.g., 'agent', 'orchestrator', 'controller')
        session_id: Optional session ID for session-aware logging

    Returns:
        Configured logger instance with appropriate handlers and formatting

    Examples:
        >>> logger = get_task_logger('agent', 'abc123')
        >>> logger.info('Agent started')

        >>> logger = get_task_logger('orchestrator')
        >>> logger.debug('Session created')
    """
    # Create consistent logger name
    if session_id:
        # Truncate session ID to first 8 chars for readability
        logger_name = f"task.{component_name}[{session_id[:8]}]"
    else:
        logger_name = f"task.{component_name}"

    # Return cached logger if available
    if logger_name in _LOGGER_CACHE:
        return _LOGGER_CACHE[logger_name]

    # Use core logging if available
    if CORE_LOGGING_AVAILABLE:
        try:
            logger = get_component_logger(logger_name)
            _LOGGER_CACHE[logger_name] = logger
            return logger
        except Exception:
            # Fall through to basic logger
            pass

    # Fallback to basic logger configuration
    logger = logging.getLogger(logger_name)

    # Only configure if not already configured
    if not logger.handlers:
        # Set level from environment or default to INFO
        log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
        logger.setLevel(getattr(logging, log_level, logging.INFO))

        # Add console handler with simple format
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        # Prevent propagation to avoid duplicate logs
        logger.propagate = False

    _LOGGER_CACHE[logger_name] = logger
    return logger


def setup_logging(log_level: str = 'INFO', log_file: Optional[str] = None) -> None:
    """
    Basic logging setup.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path for logging
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Basic configuration
    handlers = [logging.StreamHandler()]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=handlers,
        force=True
    )

    # Silence noisy libraries
    configure_library_loggers()

def configure_library_loggers():
    """Configure logging levels for third-party libraries."""
    # Silence noisy libraries
    noisy_loggers = [
        'urllib3',
        'httpx',
        'httpcore',
        'selenium',
        'PIL',
        'matplotlib',
        'asyncio',
        'filelock',
        'fontTools',
        'pinecone',
        'google.auth',
        'google.api_core',
        'google.cloud',
        'googleapiclient',
        'openai',
        'anthropic'
    ]

    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # Special handling for very noisy loggers
    logging.getLogger('httpx._client').setLevel(logging.ERROR)
    logging.getLogger('httpcore._trace').setLevel(logging.ERROR)

    # Disable propagation for task loggers to avoid duplicates
    logging.getLogger('task').propagate = False

# Removed compatibility no-ops - these functions are no longer needed:
# - silence_metadata_logs()
# - fix_httpx_logging()
# - enable_log_deduplication()
# - reset_log_deduplication_cache()
# - optimize_logging_for_agent_system()
# If you're using any of these, please remove the calls or use configure_library_loggers() directly