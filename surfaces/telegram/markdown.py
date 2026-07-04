"""Platform-agnostic markdown utilities for message formatting."""

from typing import Optional, Tuple, List

__all__ = [
    'escape_markdown',
    'escape_markdown_v2',
    'format_message_with_markdown',
    'format_user_mention',
    'safe_markdown_message',
    'format_command_list',
    'format_mode_description',
    'format_help_section',
    'escape_html',
    'is_already_escaped',
    'format_role_name',
    'markdown_to_html'
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


def format_user_mention(username: Optional[str], full_name: str) -> str:
    """Format user mention for group chats."""
    if username:
        return f"@{username}"
    return escape_markdown(full_name)


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


def format_command_list(commands: List[Tuple[str, str]]) -> str:
    """Format a list of commands with proper escaping."""
    formatted = []
    for cmd, desc in commands:
        # Preserve emojis and special characters in description
        desc = desc.replace('_', '\\_').replace('*', '\\*').replace('`', '\\`')
        # Keep emojis intact
        desc_escaped = ''
        for c in desc:
            if c.isalnum() or c.isspace() or ord(c) > 127:  # Keep unicode chars (emojis)
                desc_escaped += c
            elif c not in '\\_\\*\\`':  # Don't double-escape
                desc_escaped += f'\\{c}'
            else:
                desc_escaped += c
        # Use backticks to properly format commands with underscores
        formatted.append(f"• `/{cmd}` - {desc_escaped}")
    return '\n'.join(formatted)


def format_mode_description(mode: str, emoji: str, desc: str) -> str:
    """Format mode description with proper escaping."""
    # Preserve emoji and handle special characters
    mode = mode.replace('_', '\\_')
    desc = desc.replace('_', '\\_').replace('*', '\\*').replace('`', '\\`')
    return f"• `{mode}` - {emoji} {desc}"


def format_help_section(title: str, content: str) -> str:
    """Format a help section with proper escaping."""
    title = title.replace('_', '\\_').replace('*', '\\*')
    return f"\n{title}:\n{content}"


def escape_html(text: str) -> str:
    """
    Escape special characters for HTML formatting.
    Replaces characters that have special meaning in HTML.
    """
    if not text:
        return ""

    escape_map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    }

    result = ""
    for char in text:
        if char in escape_map:
            result += escape_map[char]
        else:
            result += char

    return result


def format_role_name(role: str) -> str:
    """Format a role name for display.

    Args:
        role: Role name to format

    Returns:
        str: Formatted role name
    """
    role_emojis = {
        'user': '👤',
        'admin': '🛡️',
        'moderator': '🔰',
        'super_admin': '👑',
        'den_member': '🔵'
    }

    emoji = role_emojis.get(role, '❓')
    return f"{emoji} {role}"


def markdown_to_html(text: str) -> str:
    """Convert common markdown formatting to Telegram HTML.
    
    This is useful for LLM-generated content which often uses markdown,
    but Telegram HTML is more forgiving and doesn't require escaping.
    
    Converts:
    - **bold** or __bold__ -> <b>bold</b>
    - *italic* or _italic_ -> <i>italic</i>
    - `code` -> <code>code</code>
    - ```code block``` -> <pre>code block</pre>
    - [link](url) -> <a href="url">link</a>
    
    Args:
        text: Markdown text to convert
        
    Returns:
        HTML-formatted text safe for Telegram
    """
    if not text:
        return ""
    
    import re
    
    # First, escape HTML characters to prevent injection
    html_text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    # Convert code blocks first (multiline)
    # ```language\ncode\n``` or ```code```
    html_text = re.sub(r'```(?:\w+\n)?(.*?)```', r'<pre>\1</pre>', html_text, flags=re.DOTALL)
    
    # Convert inline code
    html_text = re.sub(r'`([^`]+)`', r'<code>\1</code>', html_text)
    
    # Convert bold (** or __)
    html_text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', html_text)
    html_text = re.sub(r'__(.+?)__', r'<b>\1</b>', html_text)
    
    # Convert italic (* or _) - be careful not to catch URLs or filenames
    # Only convert if surrounded by spaces or at start/end
    html_text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'<i>\1</i>', html_text)
    html_text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', html_text)
    
    # Convert links [text](url)
    html_text = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<a href="\2">\1</a>', html_text)
    
    # Convert strikethrough ~~text~~
    html_text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', html_text)
    
    return html_text