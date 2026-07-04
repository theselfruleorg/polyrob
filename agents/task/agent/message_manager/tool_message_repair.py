"""Tool-message sequence repair/validation (roadmap P9; code-motion from tool_call_builder.py).

The duplicate-detection, tool-message-pair repair, validation, and the top-level
`repair_and_normalize` entry point — split out of tool_call_builder.py so that file
owns tool-call *construction* and this one owns *message-sequence repair*. The names
are re-exported from tool_call_builder for backward compatibility.
"""
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import json
import logging

from modules.llm.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from agents.task.agent.message_manager.tool_call_builder import _convert_to_serializable

logger = logging.getLogger(__name__)


def detect_and_remove_duplicate_tool_calls(messages, logger=None):
    """Centralized duplicate detection and removal for tool calls.

    This function handles all duplicate detection logic in one place,
    ensuring consistent behavior across the codebase.

    Args:
        messages: List of messages to check
        logger: Optional logger for debugging

    Returns:
        Tuple of (cleaned_messages, duplicates_removed)
    """
    # AIMessage imported at module level from modules.llm.messages

    cleaned_messages = []
    last_ai_message = None
    duplicates_removed = 0

    for i, msg in enumerate(messages):
        # Check if this is an AIMessage with tool_calls
        is_ai_with_tools = (
            isinstance(msg, AIMessage) and
            hasattr(msg, 'tool_calls') and
            msg.tool_calls
        )

        # Check for TRUE duplicates: same content AND same tool call IDs
        if is_ai_with_tools and last_ai_message is not None:
            # Get tool call IDs from both messages
            current_ids = set()
            for tc in msg.tool_calls:
                tc_id = tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None)
                if tc_id:
                    current_ids.add(tc_id)

            last_ids = set()
            for tc in last_ai_message.tool_calls:
                tc_id = tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None)
                if tc_id:
                    last_ids.add(tc_id)

            # Only remove if BOTH content and tool call IDs match (true duplicate)
            content_match = msg.content == last_ai_message.content
            ids_match = current_ids == last_ids and len(current_ids) > 0

            if content_match and ids_match:
                if logger:
                    logger.warning(
                        f"[DUPLICATE_DETECTION] Removing duplicate consecutive AIMessage "
                        f"with identical content and tool_calls at index {i} (IDs: {current_ids})"
                    )
                duplicates_removed += 1
                continue
            elif logger and is_ai_with_tools:
                logger.debug(
                    f"[DUPLICATE_DETECTION] Consecutive AIMessages at index {i} are NOT duplicates "
                    f"(content_match={content_match}, ids_match={ids_match})"
                )

        # Check for duplicate tool_call IDs within the same message
        if is_ai_with_tools:
            seen_ids = set()
            unique_tool_calls = []
            for tool_call in msg.tool_calls:
                tc_id = tool_call.get('id') if isinstance(tool_call, dict) else getattr(tool_call, 'id', None)
                if tc_id and tc_id not in seen_ids:
                    seen_ids.add(tc_id)
                    unique_tool_calls.append(tool_call)
                elif logger:
                    logger.warning(
                        f"[DUPLICATE_DETECTION] Removing duplicate tool_call ID {tc_id} "
                        f"within same message at index {i}"
                    )

            # Update tool_calls if duplicates were found
            if len(unique_tool_calls) < len(msg.tool_calls):
                msg.tool_calls = unique_tool_calls
                if logger:
                    logger.info(
                        f"[DUPLICATE_DETECTION] Reduced tool_calls from {len(msg.tool_calls)} "
                        f"to {len(unique_tool_calls)} in message at index {i}"
                    )

        cleaned_messages.append(msg)
        # Update last_ai_message tracker if this was an AI message with tools
        if is_ai_with_tools:
            last_ai_message = msg
        else:
            # Reset when we encounter a non-AI message (allows pattern: AI -> User -> AI with same tools)
            last_ai_message = None

    if logger and duplicates_removed > 0:
        logger.info(
            f"[DUPLICATE_DETECTION] Total duplicates removed: {duplicates_removed}, "
            f"messages reduced from {len(messages)} to {len(cleaned_messages)}"
        )

    return cleaned_messages, duplicates_removed




def repair_tool_message_pairs(
    messages: List[Any],
    logger: Optional[logging.Logger] = None
) -> Tuple[List[Any], Dict[str, Any]]:
    """SIMPLIFIED repair: ensure AIMessage+tool_calls followed by ToolMessages.

    PHILOSOPHY: Trust atomically-added messages. Just fix ordering if needed.
    NO deletion, NO complex lookahead, NO jumping.

    Args:
        messages: List of messages to validate and repair
        logger: Optional logger for diagnostics

    Returns:
        Tuple of (repaired_messages, report_dict)
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # Message types imported at module level from modules.llm.messages

    # Remove duplicates
    messages, duplicates_removed = detect_and_remove_duplicate_tool_calls(messages, logger)

    # Build lookup: tool_call_id -> ToolMessage
    tool_msg_map = {}
    for msg in messages:
        if isinstance(msg, ToolMessage) and hasattr(msg, 'tool_call_id'):
            tool_msg_map[msg.tool_call_id] = msg

    # Rebuild with correct adjacency
    repaired = []
    i = 0

    while i < len(messages):
        msg = messages[i]

        if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
            # Add AIMessage
            repaired.append(msg)

            # Add its ToolMessages immediately after (adjacency)
            for tc in msg.tool_calls:
                tc_id = tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None)
                if tc_id and tc_id in tool_msg_map:
                    repaired.append(tool_msg_map[tc_id])
                    del tool_msg_map[tc_id]  # Don't add twice
                elif tc_id:
                    # ✅ FIX: Missing response is a critical bug - add placeholder to prevent message sequence errors
                    # This indicates agent.step() didn't call add_tool_response() for this tool call
                    tool_name = tc.get('name', 'unknown') if isinstance(tc, dict) else getattr(tc, 'name', 'unknown')
                    logger.error(
                        f"🚨 CRITICAL: Tool call {tc_id} ({tool_name}) has no response! "
                        f"Agent.step() failed to call add_tool_response(). "
                        f"Adding placeholder to prevent message sequence validation error."
                    )

                    # Add placeholder ToolMessage to prevent LLM API rejection
                    # ToolMessage imported at module level from modules.llm.messages
                    placeholder = ToolMessage(
                        content=f"[ERROR: No response recorded for tool '{tool_name}'. This is a bug in the agent execution flow.]",
                        tool_call_id=tc_id
                    )
                    repaired.append(placeholder)

                    # Track this as a metric for monitoring
                    if logger:
                        logger.warning(f"Added placeholder ToolMessage for orphaned tool_call {tc_id}")

        elif isinstance(msg, ToolMessage):
            # Already added with its AIMessage, skip
            pass

        else:
            # Regular message
            repaired.append(msg)

        i += 1  # Always increment, no jumping!

    report = {
        'duplicates_removed': duplicates_removed,
        'input_count': len(messages),
        'output_count': len(repaired)
    }

    # CRITICAL: Log if we deleted messages
    if len(repaired) < len(messages):
        delta = len(messages) - len(repaired)
        logger.info(f"Reordered {delta} ToolMessages for adjacency (expected behavior)")

    return repaired, report


def validate_tool_message_pairs(messages: List[Any], logger: Optional[logging.Logger] = None) -> bool:
    """SIMPLIFIED: Quick check for orphan tool_calls.

    Args:
        messages: List of messages to validate
        logger: Optional logger for warnings

    Returns:
        True if all tool calls have matching tool messages
    """
    # Message types imported at module level from modules.llm.messages

    pending = set()
    for msg in messages:
        if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
            for tc in msg.tool_calls:
                tc_id = tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None)
                if tc_id:
                    pending.add(tc_id)
        elif isinstance(msg, ToolMessage) and hasattr(msg, 'tool_call_id'):
            pending.discard(msg.tool_call_id)

    return len(pending) == 0  # All tool_calls have responses


def repair_and_normalize(messages: List[Any], logger: Optional[logging.Logger] = None, expect_system_message: bool = True) -> List[Any]:
    """Single-authority method for tool-call normalization, validation, placeholder creation, and duplicate removal.

    ✅ FIX (Nov 5, 2025): Updated to support repairing ONLY conversation without SystemMessage

    This method can now repair two scenarios:
    1. Full message list (foundation + conversation) - SystemMessage expected
    2. Conversation only - SystemMessage NOT expected (called from get_messages_for_llm after foundation split)

    Guarantees:
    - All tool calls are normalized to a consistent format
    - Tool message adjacency is ensured
    - Placeholders are created for missing tool responses
    - Duplicate tool calls are removed
    - Message sequence is validated
    - SystemMessage is preserved IF present

    Args:
        messages: List of messages to repair and normalize
        logger: Optional logger for warnings
        expect_system_message: If True, warn if SystemMessage missing. If False, it's normal (conversation-only repair).

    Returns:
        Repaired and normalized list of messages
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    if not messages:
        return messages

    logger.debug(f"repair_and_normalize: Input {len(messages)} messages (expect_system_message={expect_system_message})")

    # ✅ FIX #3: Preserve SystemMessage if present (but don't require it)
    # Message types imported at module level from modules.llm.messages

    system_message = None
    other_messages = []

    for msg in messages:
        if isinstance(msg, SystemMessage):
            if system_message is None:
                system_message = msg  # Keep first SystemMessage
                logger.debug("Found SystemMessage, preserving it")
            else:
                logger.warning("Multiple SystemMessages found, keeping only the first")
        else:
            other_messages.append(msg)

    # Step 1: Remove duplicate tool calls from other messages
    other_messages, _ = detect_and_remove_duplicate_tool_calls(other_messages, logger)

    # Step 2: Ensure tool_calls have correct OpenAI structure
    # OpenAI requires: {"id": "...", "type": "function", "function": {"name": "...", "arguments": "<JSON_STRING>"}}
    for msg in other_messages:
        if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
            normalized_tool_calls = []
            for tool_call in msg.tool_calls:
                if isinstance(tool_call, dict):
                    # Ensure proper OpenAI structure
                    if 'function' in tool_call:
                        # Already in OpenAI format - ensure arguments is a string
                        tool_call['type'] = 'function'
                        func_args = tool_call['function'].get('arguments')
                        if func_args is not None and not isinstance(func_args, str):
                            # Convert protobuf types and serialize to JSON string
                            serializable_args = _convert_to_serializable(func_args)
                            tool_call['function']['arguments'] = json.dumps(serializable_args)
                    elif 'name' in tool_call:
                        # Convert flat format to OpenAI nested format
                        arguments = tool_call.get('arguments', tool_call.get('args', '{}'))
                        # Ensure arguments is a JSON string, not a dict or protobuf type
                        if not isinstance(arguments, str):
                            # Convert protobuf types (MapComposite, etc.) to serializable Python types
                            serializable_args = _convert_to_serializable(arguments)
                            arguments = json.dumps(serializable_args)
                        tool_call = {
                            'id': tool_call.get('id', 'call_unknown'),
                            'type': 'function',
                            'function': {
                                'name': tool_call['name'],
                                'arguments': arguments
                            }
                        }
                    normalized_tool_calls.append(tool_call)
                else:
                    # Object format - convert to dict
                    normalized_tool_calls.append(tool_call)
            msg.tool_calls = normalized_tool_calls

    # Step 3: Repair tool message pairs
    other_messages, _ = repair_tool_message_pairs(other_messages, logger)

    # Step 4: Final validation
    is_valid = validate_tool_message_pairs(other_messages, logger)
    if not is_valid:
        logger.warning("Message sequence validation failed after repair")

    # ✅ FIX (Nov 5, 2025): Reconstruct with SystemMessage if present
    if system_message:
        repaired = [system_message] + other_messages
        logger.debug("✅ SystemMessage preserved at index 0")
    else:
        # No SystemMessage - this is NORMAL when repairing conversation-only
        if expect_system_message:
            logger.warning("No SystemMessage found during repair (full message list expected)")
        else:
            logger.debug("No SystemMessage in input (conversation-only repair, this is expected)")
        repaired = other_messages

    logger.debug(f"repair_and_normalize: Completed with {len(repaired)} messages")

    # ✅ FIX (Nov 5, 2025): Validate SystemMessage position ONLY if we expect it
    if expect_system_message:
        if repaired and isinstance(repaired[0], SystemMessage):
            logger.debug("✅ SystemMessage validation passed in repair")
        else:
            logger.error("⚠️ SystemMessage NOT at index 0 after repair!")

    return repaired
