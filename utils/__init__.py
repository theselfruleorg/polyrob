"""Utility functions and helpers."""

from .time_utils import time_execution_sync, time_execution_async
from .markdown_utils import format_message_with_markdown
from .message_utils import split_long_message, format_long_message, send_long_message
from .gif_utils import (
    create_history_gif,
    get_conversation_screenshots,
    create_gif_with_retry,
    create_text_only_gif
)

# Import RateLimitManager directly to avoid circular dependency issues
# Users should import it as: from utils.rate_limit_manager import RateLimitManager
# Not included here to prevent circular imports with core modules
from .user_utils import (
    extract_user_data,
    validate_email,
    validate_wallet_address,
    generate_user_id,
    is_valid_hash_id,
    get_id_type,
    format_user_display_name
)

__all__ = [
    'time_execution_sync',
    'time_execution_async',
    'format_message_with_markdown',
    'split_long_message',
    'format_long_message',
    'send_long_message',
    'create_history_gif',
    'get_conversation_screenshots',
    'create_gif_with_retry',
    'create_text_only_gif',
    'extract_user_data',
    'validate_email',
    'validate_wallet_address',
    'generate_user_id',
    'is_valid_hash_id',
    'get_id_type',
    'format_user_display_name'
]