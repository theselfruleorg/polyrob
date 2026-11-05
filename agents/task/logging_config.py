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

# Cache for created loggers: one per component, never one per session
_LOGGER_CACHE = {}


class _SessionNameFilter(logging.Filter):
    """Writes the session onto the record's name, so a log line, the console formatter
    and ``cli/ui/log_squelch.py`` still read ``task.agent[abc12345]``."""

    def filter(self, record: logging.LogRecord) -> bool:
        session = record.__dict__.get("task_session")
        if session:
            record.name = f"{record.name}[{session}]"
        return True


_SESSION_NAME_FILTER = _SessionNameFilter()


class _SessionLogger(logging.LoggerAdapter):
    """One session's view of its component logger. It dies with the session; a logger
    named per session stays in ``logging.root.manager`` for the process life (102 M-C)."""

    @property
    def level(self) -> int:
        return self.logger.level

    def process(self, msg, kwargs):
        kwargs["extra"] = {**(kwargs.get("extra") or {}), **self.extra}
        return msg, kwargs


def get_task_logger(component_name: str, session_id: Optional[str] = None, child: Optional[str] = None):
    """
    Get a logger for task agent components.

    Integrates with core logging system when available, with automatic fallback
    to basic logging if core system is not initialized.

    Args:
        component_name: Component name (e.g., 'agent', 'orchestrator', 'controller')
        session_id: Optional session ID for session-aware logging

    Returns:
        The component's logger, or with a session id an adapter over it

    Examples:
        >>> logger = get_task_logger('agent', 'abc123')
        >>> logger.info('Agent started')

        >>> logger = get_task_logger('orchestrator')
        >>> logger.debug('Session created')
    """
    logger = _component_logger(f"task.{component_name}")
    if session_id:
        # Truncate session ID to first 8 chars for readability
        # A child id is unique: it goes on the record, never in a logger name, which
        # the logging manager keeps for the process life (102 M-C).
        label = f"{session_id[:8]}/{child}" if child else session_id[:8]
        return _SessionLogger(logger, {"task_session": label})
    return logger


def _component_logger(logger_name: str) -> logging.Logger:
    # Return cached logger if available
    if logger_name in _LOGGER_CACHE:
        return _LOGGER_CACHE[logger_name]

    # Use core logging if available
    if CORE_LOGGING_AVAILABLE:
        try:
            logger = get_component_logger(logger_name)
            logger.addFilter(_SESSION_NAME_FILTER)
            _LOGGER_CACHE[logger_name] = logger
            return logger
        except Exception:
            # Fall through to basic logger
            pass

    # Fallback to basic logger configuration
    logger = logging.getLogger(logger_name)
    logger.addFilter(_SESSION_NAME_FILTER)

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
        from core.security_logging_filter import SecretScrubbingFilter
        handler.addFilter(SecretScrubbingFilter())
        logger.addHandler(handler)

        # Prevent propagation to avoid duplicate logs
        logger.propagate = False

    _LOGGER_CACHE[logger_name] = logger
    return logger


def configure_library_loggers():
    """Configure logging levels for third-party libraries.

    Delegates to core.logging.quiet_noisy_libraries (the single source of truth
    for the pin list); falls back to a local pin when core isn't importable.
    """
    if CORE_LOGGING_AVAILABLE:
        from core.logging import quiet_noisy_libraries
        quiet_noisy_libraries()
    else:
        for logger_name in ('urllib3', 'httpx', 'httpcore', 'openai', 'anthropic'):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
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