"""
Security logging filter to prevent secrets from being logged.
"""

import logging
import re
from typing import Set, Pattern

class SecretScrubbingFilter(logging.Filter):
    """Filter that scrubs sensitive information from log records."""

    # Common secret patterns to match
    SECRET_PATTERNS = [
        # API keys - various formats
        re.compile(r'(?i)api[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9+/=]{20,})["\']?'),
        re.compile(r'(?i)key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9+/=]{20,})["\']?'),
        re.compile(r'(?i)token["\']?\s*[:=]\s*["\']?([a-zA-Z0-9+/=_-]{20,})["\']?'),
        re.compile(r'(?i)secret["\']?\s*[:=]\s*["\']?([a-zA-Z0-9+/=_-]{20,})["\']?'),

        # Passwords
        re.compile(r'(?i)password["\']?\s*[:=]\s*["\']?([^\s"\']{6,})["\']?'),
        re.compile(r'(?i)passwd["\']?\s*[:=]\s*["\']?([^\s"\']{6,})["\']?'),

        # Bearer tokens
        re.compile(r'(?i)bearer\s+([a-zA-Z0-9+/=_-]{20,})'),

        # Specific service patterns
        re.compile(r'sk-[a-zA-Z0-9]{48}'),  # OpenAI API keys
        re.compile(r'claude-[a-zA-Z0-9_-]{20,}'),  # Anthropic keys
        re.compile(r'pc-[a-zA-Z0-9]{32}'),  # Pinecone keys

        # Generic base64-looking strings that might be keys
        re.compile(r'([a-zA-Z0-9+/]{32,}={0,2})'),
    ]

    # Fields that should always be scrubbed if they contain potential secrets
    SENSITIVE_FIELDS = {
        'api_key', 'apikey', 'api-key',
        'secret_key', 'secretkey', 'secret-key', 'secret',
        'password', 'passwd', 'pass',
        'token', 'access_token', 'refresh_token',
        'authorization', 'auth',
        'key', 'private_key', 'public_key',
        'client_secret', 'client_id'
    }

    def __init__(self, mask_length: int = 4, mask_char: str = '*'):
        """
        Initialize the filter.

        Args:
            mask_length: Number of characters to show at start/end
            mask_char: Character to use for masking
        """
        super().__init__()
        self.mask_length = mask_length
        self.mask_char = mask_char

    def mask_secret(self, secret: str) -> str:
        """Mask a secret string, showing only first/last few characters."""
        if len(secret) <= self.mask_length * 2:
            return self.mask_char * len(secret)

        start = secret[:self.mask_length]
        end = secret[-self.mask_length:]
        middle_length = len(secret) - (self.mask_length * 2)
        return f"{start}{self.mask_char * min(middle_length, 10)}{end}"

    def scrub_message(self, message: str) -> str:
        """Scrub secrets from a message string."""
        scrubbed = message

        # Apply all secret patterns
        for pattern in self.SECRET_PATTERNS:
            def replace_secret(match):
                full_match = match.group(0)
                if len(match.groups()) > 0:
                    secret = match.group(1)
                    masked_secret = self.mask_secret(secret)
                    return full_match.replace(secret, masked_secret)
                return self.mask_secret(full_match)

            scrubbed = pattern.sub(replace_secret, scrubbed)

        return scrubbed

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Filter method called on each log record.

        Returns:
            True to allow the record through, False to drop it
        """
        # Scrub the main message
        if hasattr(record, 'msg') and record.msg:
            if isinstance(record.msg, str):
                record.msg = self.scrub_message(record.msg)

        # Scrub formatted message
        if hasattr(record, 'getMessage'):
            try:
                original_get_message = record.getMessage

                def scrubbed_get_message():
                    msg = original_get_message()
                    return self.scrub_message(msg) if isinstance(msg, str) else msg

                record.getMessage = scrubbed_get_message
            except Exception:
                pass

        # Scrub any dictionary-like extra data
        if hasattr(record, '__dict__'):
            for key, value in record.__dict__.items():
                if isinstance(value, str) and (
                    key.lower() in self.SENSITIVE_FIELDS or
                    any(field in key.lower() for field in self.SENSITIVE_FIELDS)
                ):
                    record.__dict__[key] = self.mask_secret(value)

        return True


def install_secret_scrubbing_filter(logger_name: str = None):
    """
    Install the secret scrubbing filter on a logger.

    Args:
        logger_name: Name of logger to install on, None for root logger
    """
    logger = logging.getLogger(logger_name)

    # Check if filter is already installed
    for filter_obj in logger.filters:
        if isinstance(filter_obj, SecretScrubbingFilter):
            return

    filter_obj = SecretScrubbingFilter()
    logger.addFilter(filter_obj)


def install_global_secret_scrubbing():
    """Install secret scrubbing on the root logger to catch all logs."""
    install_secret_scrubbing_filter()


# Auto-install on import
install_global_secret_scrubbing()