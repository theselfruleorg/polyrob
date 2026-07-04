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
# Anchor to the install/repo root (this file is core/logging.py -> parent.parent),
# NOT the process CWD. setup_logging() runs at import time of `core`, before any
# config/bootstrap, so a relative Path('logs') would drop a ./logs tree into
# whatever directory the user launched from (e.g. the `rob` CLI in their repo).
DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / 'logs'
MAX_BYTES = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 5

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

def ensure_log_directory(log_dir: Path = DEFAULT_LOG_DIR) -> Path:
    """Ensure log directory exists.
    
    Args:
        log_dir: Path to log directory
        
    Returns:
        Path to log directory
    """
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
    component_name: Optional[str] = None
) -> None:
    """Set up logging with enhanced formatter."""
    try:
        numeric_level = getattr(logging, log_level.upper(), logging.INFO)
        
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
        root_logger.setLevel(numeric_level)

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
            root_logger.addHandler(file_handler)

            # Console handler with standard formatter
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(standard_formatter)
            console_handler.setLevel(numeric_level)
            root_logger.addHandler(console_handler)

            # Special handler for httpx logs
            httpx_handler = logging.StreamHandler()
            httpx_handler.setFormatter(http_formatter)
            httpx_handler.addFilter(lambda record: record.name == 'httpx')
            httpx_handler.setLevel(numeric_level)
            root_logger.addHandler(httpx_handler)

            # Add security filter to root logger to scrub secrets
            security_filter = SecretScrubbingFilter()
            root_logger.addFilter(security_filter)

            # Mark root logger as configured so we don't duplicate handlers
            _ROOT_LOGGER_CONFIGURED = True
        else:
            # Root logger already configured – simply update its level to the
            # latest requested level (if it changed) but DO NOT add handlers.
            for handler in root_logger.handlers:
                handler.setLevel(numeric_level)
         
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
                
        # Only print the initialization banner once per application lifetime.
        # Use global flag to ensure it's only printed when root logger is first configured
        global _LOGGING_BANNER_PRINTED
        if not _LOGGING_BANNER_PRINTED and not _ROOT_LOGGER_CONFIGURED:
            print(f"Logging initialized with level {log_level}")
            root_logger.info(f"Logging system initialized with level {log_level}")
            _LOGGING_BANNER_PRINTED = True
            
    except Exception as e:
        print(f"Error setting up logging: {e}")
        if logger:
            basic_handler = logging.StreamHandler()
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
            component_name=component_name
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

def initialize_task_logging() -> bool:
    """Initialize AutoV2 logging integration with core logging system.
    
    Returns:
        bool: True if integration was successful, False otherwise
    """
    try:
        # Try to import AutoV2 logging
        from agents.task.logging_config import ensure_core_logging_integration, configure_library_loggers
        
        # Ensure integration
        if ensure_core_logging_integration():
            # Configure library loggers to reduce noise
            configure_library_loggers()
            
            # Get the task logger and ensure it propagates
            task_logger = logging.getLogger('task')
            task_logger.propagate = True
            task_logger.setLevel(logging.INFO)
            
            # Log success
            root_logger = logging.getLogger()
            if root_logger.handlers:
                root_logger.info("AutoV2 logging system integrated with core logging")
            
            return True
        else:
            return False
            
    except ImportError:
        # AutoV2 module not available
        return False
    except Exception as e:
        # Log error if root logger is available
        try:
            root_logger = logging.getLogger()
            if root_logger.handlers:
                root_logger.warning(f"Failed to initialize AutoV2 logging integration: {e}")
        except:
            pass
        return False
