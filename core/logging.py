"""Location: core/logging.py"""

"""Logging configuration with comfortable, soft colors."""

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from colorama import Fore, Back, Style, init as colorama_init
from typing import Optional
import time

# Import security filter
from core.security_logging_filter import SecretScrubbingFilter

colorama_init()

# Constants
MAX_BYTES = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 5


def resolve_log_dir() -> Path:
    """Runtime log directory: ``POLYROB_LOG_DIR`` → ``<data_home>/logs``.

    Resolved at CALL time (never at import — logging configures at import-of-
    ``core`` time, before config/bootstrap, and a module-level constant would
    freeze the pre-bootstrap environment). Until T11 (2026-07-16) this was
    ``<install_root>/logs`` — a runtime write into the code tree, which breaks
    under a pip/site-packages install and is exactly what core/runtime_paths
    isolation exists to prevent. Never CWD-relative (the N1 pollution class).
    """
    import os
    env = os.getenv("POLYROB_LOG_DIR")
    if env and env.strip():
        return Path(env).expanduser()
    from core.runtime_paths import resolve_data_home
    return resolve_data_home() / "logs"

# ---------------------------------------------------------------------------
# Internal state flag to ensure we only configure the *root* logger once.
# Re-entering setup_logging for component loggers should NOT re-attach handlers
# to the root logger, otherwise each log record will be emitted N times.
# ---------------------------------------------------------------------------
_ROOT_LOGGER_CONFIGURED = False
_LOGGING_BANNER_PRINTED = False  # Global flag to ensure banner is only printed once

class ComfyFormatter(logging.Formatter):
    """Formatter with enhanced terminal theme."""
    
    # Update color scheme for better visibility
    COLORS = {
        'DEBUG': Fore.LIGHTBLACK_EX,
        'INFO': Fore.LIGHTBLUE_EX,                                    # Brighter blue
        'SUCCESS': Fore.LIGHTGREEN_EX,                               # Brighter green
        'WARNING': Style.BRIGHT + Back.YELLOW + Fore.BLACK,
        'ERROR': Style.BRIGHT + Back.RED + Fore.WHITE,
        'CRITICAL': Style.BRIGHT + Back.MAGENTA + Fore.WHITE
    }

    # Enhanced component colors
    TIME_COLOR = Fore.LIGHTBLACK_EX + Style.DIM                     # Dimmed timestamp
    NAME_COLOR = Fore.LIGHTCYAN_EX                                  # Brighter cyan
    MSG_COLOR = Fore.WHITE                                          # White for better readability
    PATH_COLOR = Fore.LIGHTGREEN_EX                                 # Brighter green
    
    # Add status emojis for better visual scanning
    STATUS_EMOJIS = {
        'initialized': '✨',
        'registered': '📝',
        'connected': '🔗',
        'started': '▶️',
        'stopped': '⏹️',
        'warning': '⚠️',
        'error': '❌',
        'success': '✅',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reset = Style.RESET_ALL
        self._last_log = {}
        self._buffer_timeout = 0.5  # Reduced from 1.0 for more responsive logging
        
    def format(self, record):
        """Format log record with enhanced styling and timestamps."""
        try:
            if not sys.stdout.isatty():
                return super().format(record)

            # Create unique key for deduplication
            log_key = f"{record.name}:{record.levelname}:{record.msg}"
            current_time = time.time()

            # Check for duplicate messages within buffer timeout
            if self._should_skip_duplicate(log_key, current_time):
                return ""

            # Format standard message
            timestamp = self._format_timestamp(record)
            name = self._format_component_name(record)
            level_color = self.COLORS.get(record.levelname, self.MSG_COLOR)
            emoji = self._get_status_emoji(record.msg)
            message = self._format_message(record, level_color)

            # Combine all elements
            formatted = f"{timestamp}{level_color}{record.levelname:<8}{self.reset} {name}: {emoji}{message}{self.reset}"

            return formatted

        except Exception:
            return super().format(record)

    def _should_skip_duplicate(self, log_key: str, current_time: float) -> bool:
        """Check if message should be skipped due to deduplication."""
        last_time = self._last_log.get(log_key, 0)
        if current_time - last_time < self._buffer_timeout:
            return True
        self._last_log[log_key] = current_time
        return False

    def _format_timestamp(self, record) -> str:
        """Format timestamp with color and microsecond precision."""
        # Get full timestamp with microseconds
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.created))
        if hasattr(record, 'msecs'):
            timestamp = f"{timestamp}.{int(record.msecs):03d}"
        return f"{self.TIME_COLOR}[{timestamp}]{self.reset} "

    def _format_component_name(self, record) -> str:
        """Format component name with proper padding and color."""
        component_parts = record.name.split('.')
        
        # Special handling for AutoV2 loggers
        if record.name.startswith('task.'):
            if '[' in record.name and ']' in record.name:
                # This is a session-specific logger like "task.agent[session_id]"
                base_component = record.name.split('[')[0].split('.')[-1]  # Get 'agent' from 'task.agent[...]'
                session_part = record.name.split('[')[1].split(']')[0]     # Get session_id from '[session_id]'
                # Truncate session ID for display
                if len(session_part) > 8:
                    session_part = session_part[:8] + "..."
                component_name = f"{base_component}[{session_part}]"
            else:
                # General AutoV2 component
                component_name = f"task.{component_parts[-1]}" if len(component_parts) > 1 else record.name
        elif len(component_parts) > 1:
            component_name = f"{component_parts[-2]}.{component_parts[-1]}"
        else:
            component_name = record.name
            
        return f"{self.NAME_COLOR}{component_name:<25}{self.reset}"

    def _get_status_emoji(self, msg: str) -> str:
        """Get appropriate emoji for message type."""
        msg_lower = str(msg).lower()
        
        # AutoV2-specific emojis
        if 'session' in msg_lower:
            if 'created' in msg_lower or 'starting' in msg_lower:
                return "🎬 "
            elif 'completed' in msg_lower or 'finished' in msg_lower:
                return "🏁 "
            elif 'running' in msg_lower:
                return "▶️ "
        
        if 'browser' in msg_lower:
            return "🌐 "
        if 'agent' in msg_lower and ('created' in msg_lower or 'initialized' in msg_lower):
            return "🤖 "
        if 'task' in msg_lower:
            return "📋 "
        if 'planning' in msg_lower or 'planner' in msg_lower:
            return "🧠 "
        if 'executing' in msg_lower or 'executor' in msg_lower:
            return "⚡ "
        
        # General emojis
        for status, emoji in self.STATUS_EMOJIS.items():
            if status in msg_lower:
                return f"{emoji} "
        return ""

    def _format_message(self, record, level_color: str) -> str:
        """Format the message with appropriate styling."""
        msg = str(record.msg)

        # Handle special message types
        if record.levelname in ('WARNING', 'ERROR', 'CRITICAL'):
            return f"{level_color}{msg}"

        # Add highlighting for important keywords
        for keyword in ['success', 'failed', 'error', 'warning']:
            if keyword in msg.lower():
                msg = msg.replace(keyword, f"{level_color}{keyword}{self.reset}")

        return msg


# Third-party loggers that chatter at INFO (httpx logs EVERY request). Pinned to
# WARNING at setup so a root-level change can never re-surface them on an
# interactive terminal. Single source of truth — agents/task/logging_config's
# configure_library_loggers delegates here.
_NOISY_LIBRARY_LOGGERS = (
    'urllib3', 'httpx', 'httpcore', 'selenium', 'PIL', 'matplotlib',
    'asyncio', 'filelock', 'fontTools', 'pinecone', 'google.auth',
    'google.api_core', 'google.cloud', 'googleapiclient', 'openai', 'anthropic',
)


def quiet_noisy_libraries() -> None:
    """Pin noisy third-party loggers to WARNING (httpx._client/httpcore._trace to ERROR)."""
    for name in _NOISY_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger('httpx._client').setLevel(logging.ERROR)
    logging.getLogger('httpcore._trace').setLevel(logging.ERROR)

# Add this new formatter class after ComfyFormatter
class HTTPFormatter(logging.Formatter):
    """Special formatter for HTTP requests."""
    
    def format(self, record):
        """Format HTTP request logs."""
        try:
            # Check if this is an httpx log
            if record.name == 'httpx':
                # Extract request details if available
                if hasattr(record, 'args') and len(record.args) >= 5:
                    method, url, _, status_code, reason = record.args
                    return f"[{self.formatTime(record)}] {record.levelname} {record.name}: HTTP {method} {url} {status_code} {reason}"
            
            # Fall back to default formatting
            return super().format(record)
        except Exception:
            return super().format(record)

class DynamicStderrHandler(logging.StreamHandler):
    """A StreamHandler that resolves ``sys.stderr`` at EMIT time.

    The stdlib handler captures the stream OBJECT at construction — ours is
    constructed at ``import core``, before the CLI REPL wraps stdio with
    prompt_toolkit's ``patch_stdout``. A captured reference writes straight
    past the REPL's screen coordination and corrupts the pinned prompt region
    (ghost frames / stranded status rows). Resolving dynamically routes any
    record through the active proxy, which prints cleanly ABOVE the prompt.
    """

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        # Identity is dynamic by design — setStream()/rebinding is a no-op.
        pass


def ensure_log_directory(log_dir: Optional[Path] = None) -> Path:
    """Ensure log directory exists.

    Args:
        log_dir: Path to log directory (default: :func:`resolve_log_dir`)

    Returns:
        Path to log directory
    """
    if log_dir is None:
        log_dir = resolve_log_dir()
    try:
        # Ensure we don't have nested logs directories
        if 'logs' in str(log_dir.parent):
            # Already in a logs directory, use that
            log_dir = log_dir.parent
        
        # Create directory if it doesn't exist
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    except Exception as e:
        print(f"Error creating log directory: {e}")
        # Fallback to current directory
        return Path('.')

def setup_logging(
    log_level: str = 'INFO',
    log_file: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    component_name: Optional[str] = None,
    *,
    console_level: Optional[str] = None,
    configure_root: bool = True,
) -> None:
    """Set up logging with enhanced formatter.

    configure_root=False (used by get_component_logger) configures ONLY the
    named component logger: it must never touch the root logger's or the root
    handlers' levels. Historically it did — every first-seen per-session task
    logger bounced the CLI's ERROR console back to INFO, leaking raw httpx
    lines into the REPL and corrupting the prompt region.

    ``console_level`` (default: ``log_level``) governs the console and httpx
    stderr sinks only; the file sink (``bot.log``) always follows
    ``log_level``. The root logger itself is set to the MOST VERBOSE of the
    two (min numeric level) so neither sink is starved of records it should
    receive — e.g. ``log_level="INFO", console_level="ERROR"`` keeps
    ``bot.log`` at INFO while the terminal only shows ERROR+.
    """
    try:
        numeric_level = getattr(logging, log_level.upper(), logging.INFO)
        console_numeric = (
            getattr(logging, console_level.upper(), numeric_level)
            if console_level is not None else numeric_level
        )
        # Root must sit at the MOST VERBOSE sink (min numeric) or records are
        # dropped before any handler can see them.
        root_numeric = min(numeric_level, console_numeric)
        
        # Create our formatters
        standard_formatter = ComfyFormatter(
            f'[%(asctime)s] %(levelname)s %(name)s: %(message)s',
            datefmt='%H:%M:%S'
        )
        
        http_formatter = HTTPFormatter(
            '[%(asctime)s] %(levelname)s %(name)s: %(message)s',
            datefmt='%H:%M:%S'
        )
        
        # -------------------------------------------------------------------
        # Configure the *root* logger only once. Subsequent invocations coming
        # from get_component_logger should merely tweak logger levels or add
        # component-specific file handlers – they MUST NOT attach additional
        # handlers to the root logger, otherwise every log record will be
        # emitted once per extra handler, leading to the huge duplication we
        # observed in bot.log.
        # -------------------------------------------------------------------
        root_logger = logging.getLogger()
        if configure_root:
            root_logger.setLevel(root_numeric)

        global _ROOT_LOGGER_CONFIGURED
        
        # Ensure log directory exists (needed for both root and component loggers)
        log_dir = ensure_log_directory()

        if not _ROOT_LOGGER_CONFIGURED:
            # Clear any pre-existing handlers to start from a clean state
            root_logger.handlers.clear()

            # Create file handler for bot.log that will capture ALL messages
            log_path = log_dir / "bot.log"

            file_handler = RotatingFileHandler(
                filename=str(log_path),
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding='utf-8'
            )
            file_handler.setFormatter(standard_formatter)
            file_handler.setLevel(numeric_level)
            file_handler._polyrob_sink = 'file'
            root_logger.addHandler(file_handler)

            # Console handler with standard formatter (late-binding stderr)
            console_handler = DynamicStderrHandler()
            console_handler.setFormatter(standard_formatter)
            console_handler.setLevel(console_numeric)
            console_handler._polyrob_sink = 'console'
            # httpx records have a dedicated handler below — without this filter
            # every visible request line was emitted twice (F7).
            console_handler.addFilter(lambda record: record.name != 'httpx')
            root_logger.addHandler(console_handler)

            # Special handler for httpx logs (late-binding stderr)
            httpx_handler = DynamicStderrHandler()
            httpx_handler.setFormatter(http_formatter)
            httpx_handler.addFilter(lambda record: record.name == 'httpx')
            httpx_handler.setLevel(console_numeric)
            httpx_handler._polyrob_sink = 'httpx'
            root_logger.addHandler(httpx_handler)

            # Add security filter to root logger to scrub secrets
            security_filter = SecretScrubbingFilter()
            root_logger.addFilter(security_filter)

            # Pin noisy libraries at first config — the CLI never runs the
            # server's initialize_agents path, so this is its only pin point.
            quiet_noisy_libraries()

            # Mark root logger as configured so we don't duplicate handlers
            _ROOT_LOGGER_CONFIGURED = True
        else:
            # Root logger already configured. Only an EXPLICIT setup call may
            # re-level its handlers; component-logger creation must not.
            if configure_root:
                for handler in root_logger.handlers:
                    sink = getattr(handler, '_polyrob_sink', 'console')
                    handler.setLevel(
                        numeric_level if sink == 'file' else console_numeric
                    )
         
        # Configure component logger if specified
        if logger and logger != root_logger:
            logger.setLevel(numeric_level)
            
            # Make sure component loggers propagate to root
            logger.propagate = True
            
            # If a specific log_file is provided for this component
            if log_file and component_name:
                component_log_path = log_dir / f"{component_name}.{log_file}"
                component_file_handler = RotatingFileHandler(
                    filename=str(component_log_path),
                    maxBytes=MAX_BYTES,
                    backupCount=BACKUP_COUNT,
                    encoding='utf-8'
                )
                component_file_handler.setFormatter(standard_formatter)
                component_file_handler.setLevel(numeric_level)
                logger.addHandler(component_file_handler)

    except Exception as e:
        print(f"Error setting up logging: {e}")
        if logger:
            basic_handler = DynamicStderrHandler()
            basic_formatter = logging.Formatter('[%(asctime)s] %(levelname)s %(name)s: %(message)s')
            basic_handler.setFormatter(basic_formatter)
            logger.addHandler(basic_handler)

def get_component_logger(
    component_name: str,
    log_level: str = 'INFO',
    log_file: Optional[str] = None,
    propagate: bool = True  # Changed default to True to ensure messages go to root logger
) -> logging.Logger:
    """Get a logger for a specific component.
    
    Args:
        component_name: Name of the component
        log_level: Logging level (default: INFO)
        log_file: Optional log file path
        propagate: Whether to propagate to parent loggers (default: True)
        
    Returns:
        Logger configured for the component
    """
    logger = logging.getLogger(component_name)
    
    # Only configure if not already configured or handlers are missing
    if not logger.handlers:
        setup_logging(
            log_level=log_level,
            log_file=log_file,
            logger=logger,
            component_name=component_name,
            configure_root=False,
        )
    
    # Ensure propagation is set correctly - this is crucial for AutoV2 loggers
    logger.propagate = propagate
    
    # Special handling for AutoV2 loggers to ensure they integrate properly
    if component_name.startswith('task.'):
        # Set appropriate log level for AutoV2 components
        if '[' in component_name:
            # This is a session-specific logger, set to INFO to capture session activity
            logger.setLevel(logging.INFO)
        else:
            # This is a general AutoV2 component logger
            logger.setLevel(logging.INFO)
        
        # Ensure propagation for AutoV2 loggers
        logger.propagate = True
        
        # Log that we've configured an AutoV2 logger
        if not hasattr(logger, '_task_configured'):
            root_logger = logging.getLogger()
            if root_logger.handlers:  # Only log if root logger is set up
                root_logger.debug(f"Configured AutoV2 logger: {component_name}")
            logger._task_configured = True
    
    return logger
