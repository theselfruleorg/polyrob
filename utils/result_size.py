"""Shared utilities for managing large result sizes.

This module provides consistent truncation and offloading behavior
across all tools (browser, MCP, etc.).

FIX (Jan 2026): Created to prevent context overflow from large tool responses.
MCP responses were causing 60K+ token additions per step without any limits.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Shared limits (used by browser, MCP, etc.)
# These match the browser limits for consistency
MAX_RESULT_CHARS = 25000
MAX_RESULT_TOKENS = 6000  # ~4 chars per token


def estimate_tokens(content: str) -> int:
    """Estimate token count from content.

    Uses simple heuristic of ~4 chars per token.
    This is approximate but sufficient for limit checking.

    Args:
        content: String content to estimate

    Returns:
        Estimated token count
    """
    return len(content) // 4


def should_truncate(content: str) -> bool:
    """Check if content exceeds size limits.

    Args:
        content: Content to check

    Returns:
        True if content should be truncated/offloaded
    """
    return len(content) > MAX_RESULT_CHARS


def truncate_with_notice(
    content: str,
    source: str = "content",
    max_chars: int = MAX_RESULT_CHARS
) -> str:
    """Truncate content with notice about what was removed.

    Args:
        content: Content to truncate
        source: Description of content source for notice
        max_chars: Maximum characters to keep (default: MAX_RESULT_CHARS)

    Returns:
        Truncated content with notice, or original if under limit
    """
    if len(content) <= max_chars:
        return content

    truncated = content[:max_chars]
    removed = len(content) - max_chars
    removed_tokens = removed // 4

    return (
        f"{truncated}\n\n"
        f"[... {removed:,} more chars (~{removed_tokens:,} tokens) truncated from {source}. "
        f"Use pagination, filtering, or navigate to sub-sections for more detail.]"
    )


def smart_truncate_structure(
    data: Any,
    max_items: int = 5
) -> Any:
    """Truncate data structure while preserving its shape.

    Intelligently truncates lists and dicts to show representative samples.

    Args:
        data: Data structure to truncate (dict, list, or other)
        max_items: Maximum items to keep in lists/dicts

    Returns:
        Truncated data structure
    """
    if isinstance(data, list):
        if len(data) > max_items:
            return data[:max_items]
        return data
    elif isinstance(data, dict):
        # Handle common response patterns
        if 'items' in data and isinstance(data['items'], list):
            return {
                **data,
                'items': data['items'][:max_items],
                '_truncated': True,
                '_original_count': len(data['items'])
            }
        if 'data' in data:
            return {
                **data,
                'data': smart_truncate_structure(data['data'], max_items)
            }
        if 'results' in data and isinstance(data['results'], list):
            return {
                **data,
                'results': data['results'][:max_items],
                '_truncated': True,
                '_original_count': len(data['results'])
            }
        # Generic dict truncation
        if len(data) > max_items:
            keys = list(data.keys())[:max_items]
            return {k: data[k] for k in keys}
    return data


def describe_structure(data: Any) -> str:
    """Generate human-readable description of data structure.

    Args:
        data: Data to describe

    Returns:
        Human-readable summary string
    """
    if isinstance(data, list):
        if len(data) == 0:
            return "Empty list"
        first_type = type(data[0]).__name__
        return f"List with {len(data)} {first_type} items"
    elif isinstance(data, dict):
        keys = list(data.keys())
        if len(keys) <= 5:
            return f"Object with keys: {', '.join(str(k) for k in keys)}"
        return f"Object with {len(keys)} keys: {', '.join(str(k) for k in keys[:5])}..."
    elif isinstance(data, str):
        return f"String ({len(data):,} chars)"
    else:
        return f"Type: {type(data).__name__}"


def save_large_result(
    result: Any,
    workspace_dir: Optional[Union[str, Path]],
    prefix: str = "result",
    subfolder: str = "tool_outputs"
) -> Optional[Path]:
    """Save large result to file in workspace.

    Args:
        result: Result data to save (must be JSON-serializable)
        workspace_dir: Workspace directory path (None = no save)
        prefix: Filename prefix
        subfolder: Subfolder within workspace/data/

    Returns:
        Path to saved file, or None if save failed/skipped
    """
    if not workspace_dir:
        return None

    try:
        workspace_path = Path(workspace_dir)
        timestamp = datetime.now().strftime("%H%M%S")
        filename = f"{prefix}_{timestamp}.json"

        # Create output directory
        output_dir = workspace_path / "data" / subfolder
        output_dir.mkdir(parents=True, exist_ok=True)

        filepath = output_dir / filename

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, default=str, ensure_ascii=False)

        logger.info(f"Saved large result to: {filepath}")
        return filepath

    except Exception as e:
        logger.warning(f"Failed to save large result: {e}")
        return None


def format_large_result_message(
    result: Any,
    result_str: str,
    filepath: Optional[Path] = None,
    max_preview_chars: int = 2000
) -> str:
    """Format a large result into a context-friendly message.

    Creates a summary with structure description, file reference, and preview.

    Args:
        result: Original result data
        result_str: Full JSON string of result
        filepath: Path where full result was saved (optional)
        max_preview_chars: Maximum chars for preview section

    Returns:
        Formatted message suitable for LLM context
    """
    # Create truncated preview
    preview = smart_truncate_structure(result, max_items=5)
    preview_str = json.dumps(preview, indent=2, default=str, ensure_ascii=False)
    if len(preview_str) > max_preview_chars:
        preview_str = preview_str[:max_preview_chars] + "\n..."

    # Build message
    parts = [
        "[LARGE RESULT - TRUNCATED FOR CONTEXT]",
        "",
        f"**Size:** {len(result_str):,} chars (~{len(result_str)//4:,} tokens)",
        f"**Structure:** {describe_structure(result)}",
    ]

    if filepath:
        parts.append(f"**Full data saved to:** {filepath}")
        parts.append("")
        parts.append("Use `read_file` tool on the path above to access full data.")

    parts.extend([
        "",
        "**Preview (truncated):**",
        "```json",
        preview_str,
        "```"
    ])

    return "\n".join(parts)


def process_tool_result(
    result: Any,
    workspace_dir: Optional[Union[str, Path]] = None,
    prefix: str = "result",
    max_inline_chars: int = MAX_RESULT_CHARS
) -> Tuple[str, bool, Optional[Path]]:
    """Process tool result with automatic truncation/offloading.

    Main entry point for processing any tool result. Handles:
    - Small results: Returns as-is
    - Large results: Saves to file and returns summary

    Args:
        result: Tool result (any JSON-serializable type)
        workspace_dir: Workspace for saving large results
        prefix: Filename prefix for saved files
        max_inline_chars: Maximum chars to keep inline

    Returns:
        Tuple of (formatted_result, was_truncated, filepath)
    """
    # Convert to string for size check
    try:
        result_str = json.dumps(result, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        # Fallback for non-serializable
        result_str = str(result)
        logger.warning(f"Result not JSON-serializable, using str(): {e}")

    # Check if truncation needed
    if len(result_str) <= max_inline_chars:
        return result_str, False, None

    # Save full result to file
    filepath = save_large_result(
        result,
        workspace_dir,
        prefix=prefix,
        subfolder="mcp_outputs"
    )

    # Format truncated message
    formatted = format_large_result_message(
        result,
        result_str,
        filepath=filepath
    )

    logger.info(
        f"Truncated large result: {len(result_str):,} chars -> "
        f"{len(formatted):,} chars (saved to {filepath})"
    )

    return formatted, True, filepath
