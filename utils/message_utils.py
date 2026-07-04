"""Platform-agnostic message utilities."""

import logging
import asyncio
from typing import List, Optional, Dict, Any

from .markdown_utils import (
    format_message_with_markdown,
    safe_markdown_message,
    escape_markdown
)

logger = logging.getLogger(__name__)

__all__ = [
    'split_long_message',
    'format_long_message',
    'safe_markdown_message'
]


def split_long_message(
    text: str,
    max_length: int = 4096,
    split_on_newline: bool = True
) -> List[str]:
    """Split a long message into smaller parts.

    Args:
        text: The message text to split
        max_length: Maximum length for each part
        split_on_newline: Whether to prefer splitting on newlines

    Returns:
        List of message parts
    """
    if len(text) <= max_length:
        return [text]

    parts = []

    if split_on_newline:
        # Split by paragraphs while preserving formatting
        current_part = ""

        # Split by newlines first
        lines = text.split('\n')

        for line in lines:
            if len(current_part + line + '\n') > (max_length - 100):  # Leave some buffer
                if current_part:
                    parts.append(current_part.rstrip())
                current_part = line + '\n'
            else:
                current_part += line + '\n'

        if current_part:
            parts.append(current_part.rstrip())
    else:
        # Simple split by max length
        while text:
            if len(text) <= max_length:
                parts.append(text)
                break

            # Find a good breaking point
            split_point = max_length

            # Try to break on a space
            last_space = text.rfind(' ', 0, max_length)
            if last_space > max_length * 0.8:  # Only if it's reasonably far
                split_point = last_space

            parts.append(text[:split_point])
            text = text[split_point:].lstrip()

    return parts


def format_long_message(
    text: str,
    max_length: int = 4096,
    use_markdown: bool = True
) -> List[str]:
    """Format and split a long message.

    Args:
        text: The message text
        max_length: Maximum length for each part
        use_markdown: Whether to apply markdown formatting

    Returns:
        List of formatted message parts
    """
    if use_markdown:
        text = format_message_with_markdown(text)

    return split_long_message(text, max_length)


async def send_long_message(
    send_func: Any,  # Callable that sends a message
    text: str,
    parse_mode: Optional[str] = "MarkdownV2",
    **kwargs
) -> List[Any]:
    """Generic function to send long messages through any platform.

    Args:
        send_func: The function to call for sending (platform-specific)
        text: The message text
        parse_mode: Parse mode for the message
        **kwargs: Additional arguments for send_func

    Returns:
        List of sent message results
    """
    results = []
    parts = split_long_message(text)

    for part in parts:
        try:
            result = await send_func(
                text=part,
                parse_mode=parse_mode,
                **kwargs
            )
            results.append(result)

            if len(parts) > 1:
                await asyncio.sleep(0.5)  # Small delay between messages

        except Exception as e:
            logger.error(f"Error sending message part: {e}")
            # Try without parse mode as fallback
            try:
                result = await send_func(
                    text=part,
                    parse_mode=None,
                    **kwargs
                )
                results.append(result)
            except Exception as fallback_error:
                logger.error(f"Fallback send also failed: {fallback_error}")
                results.append(None)

    return results