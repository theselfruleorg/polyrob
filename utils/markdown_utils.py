"""Platform-agnostic markdown utilities for message formatting."""

from typing import Optional, Tuple

__all__ = [
    'escape_markdown',
    'escape_markdown_v2',
    'format_message_with_markdown',
    'safe_markdown_message',
    'is_already_escaped'
]


def is_already_escaped(text: str) -> bool:
    """Check if a string has already been escaped for markdown.

    Args:
        text: Text to check

    Returns:
        bool: True if text appears to be already escaped
    """
    # Check for typical escape patterns
    escape_patterns = [
        '\\*', '\\_', '\\`', '\\[', '\\]', '\\(', '\\)', '\\~', '\\>',
        '\\#', '\\+', '\\-', '\\=', '\\|', '\\{', '\\}', '\\.'
    ]

    # If any of these patterns appear, it's likely already escaped
    for pattern in escape_patterns:
        if pattern in text:
            return True

    return False


def escape_markdown(text: str, allow_skip: bool = True) -> str:
    """Escape special characters for Markdown formatting.

    Args:
        text: Text to escape
        allow_skip: If True, skip escaping if text appears to be already escaped

    Returns:
        str: Escaped text
    """
    if not text:
        return ""

    # Skip if already escaped
    if allow_skip and is_already_escaped(text):
        return text

    # Characters that need escaping in Markdown
    escape_chars = '_*[]()~`>#+-=|{}.!'

    # Handle code blocks carefully - count backticks first to check for uneven backticks
    backtick_count = text.count('```')
    if backtick_count % 2 != 0:
        # Uneven number of backtick triplets - need to handle specially to avoid data loss
        # In this case, we'll escape all backticks rather than trying to parse code blocks
        escaped_text = ''
        for char in text:
            if char == '`':
                escaped_text += '\\`'
            elif char in escape_chars:
                escaped_text += f'\\{char}'
            else:
                escaped_text += char
        return escaped_text

    # If we have balanced backticks, proceed with normal processing
    # Don't escape characters inside code blocks
    parts = text.split('```')
    for i in range(0, len(parts), 2):
        # Only escape in non-code parts (even indices)
        escaped = ''
        in_word = False
        word = ''

        for char in parts[i]:
            if char.isalnum() or char in '.-':
                word += char
                in_word = True
            else:
                if in_word:
                    # Check if word is a number or contains only dots/hyphens
                    if word.replace('.', '').replace('-', '').isdigit():
                        escaped += word
                    else:
                        # Escape special chars in non-numeric words
                        for c in word:
                            if c in escape_chars:
                                escaped += f'\\{c}'
                            else:
                                escaped += c
                    word = ''
                    in_word = False

                # Handle non-word characters
                if char in escape_chars:
                    escaped += f'\\{char}'
                else:
                    escaped += char

        # Handle last word if exists
        if word:
            if word.replace('.', '').replace('-', '').isdigit():
                escaped += word
            else:
                for c in word:
                    if c in escape_chars:
                        escaped += f'\\{c}'
                    else:
                        escaped += c

        parts[i] = escaped

    # Join with original delimiter
    return '```'.join(parts)


def escape_markdown_v2(text: str, allow_skip: bool = True) -> str:
    """
    Escape special characters for MarkdownV2 formatting.
    More strict than regular Markdown escaping.

    Args:
        text: Text to escape
        allow_skip: If True, skip escaping if text appears to be already escaped

    Returns:
        str: Escaped text
    """
    if not text:
        return ""

    # Skip if already escaped
    if allow_skip and is_already_escaped(text):
        return text

    # Characters that need escaping in MarkdownV2
    escape_chars = '_*[]()~`>#+-=|{}.!'

    # Don't escape characters inside code blocks
    parts = text.split('```')
    result = []

    for i, part in enumerate(parts):
        # Skip code blocks (odd indices)
        if i % 2 == 1:
            result.append(part)
            continue

        escaped = ''
        for char in part:
            if char in escape_chars:
                escaped += f'\\{char}'
            else:
                escaped += char
        result.append(escaped)

    # Rejoin with code blocks
    return '```'.join(result)


def format_message_with_markdown(message: str, **kwargs) -> str:
    """Format a message with markdown syntax.

    Args:
        message: The message to format
        **kwargs: Additional keyword arguments

    Returns:
        The formatted message
    """
    return str(message)


def safe_markdown_message(message: str, **kwargs) -> Tuple[str, Optional[str]]:
    """Format a message with markdown and ensure it's safe.

    Args:
        message: The message to format
        **kwargs: Additional keyword arguments

    Returns:
        Tuple of (safely formatted message, parse mode)
    """
    try:
        # First check if message is already markdown-safe
        if is_already_escaped(str(message)):
            return str(message), "Markdown"

        # Try to use MarkdownV2 first with proper escaping
        escaped_text = escape_markdown_v2(str(message))
        return escaped_text, "MarkdownV2"
    except Exception:
        # Fallback to plain text if escaping fails
        return str(message), None