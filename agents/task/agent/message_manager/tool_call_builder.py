"""
Tool call builder for standardizing tool call structure across AutoV2.

This module provides a consistent interface for creating tool calls that align
with OpenAI tool call schemas. It also centralizes validation,
repair, and placeholder creation for tool message sequences.

GROK 4.1 / MCP NESTED ARGUMENTS FIX (Dec 2025):
Some LLMs (notably Grok 4.1 via OpenRouter) return nested object/array parameters
as JSON strings instead of parsed objects. For example:
  {"authors": "[\"a\", \"b\"]", "filters": "{\"key\": \"val\"}"}
instead of:
  {"authors": ["a", "b"], "filters": {"key": "val"}}

This module includes `_deep_parse_json_strings()` to recursively parse these
stringified nested structures, fixing MCP tool calling issues.
"""

from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field, asdict
from uuid import uuid4
import json
import logging

logger = logging.getLogger(__name__)


def _deep_parse_json_strings(obj: Any, depth: int = 0, max_depth: int = 10) -> Any:
    """Recursively parse JSON strings within nested structures.
    
    GROK 4.1 / MCP FIX (Dec 2025):
    Some LLMs return nested objects/arrays as JSON strings instead of parsed objects.
    This function recursively finds and parses those strings.
    
    Examples:
        '["a", "b"]' -> ["a", "b"]
        '{"key": "val"}' -> {"key": "val"}
        {"items": '["x"]'} -> {"items": ["x"]}
    
    Args:
        obj: The object to process (any type)
        depth: Current recursion depth (for safety)
        max_depth: Maximum recursion depth to prevent infinite loops
    
    Returns:
        The object with all JSON strings parsed into their Python equivalents
    """
    # Safety: prevent infinite recursion
    if depth > max_depth:
        logger.warning(f"[DEEP_PARSE] Max depth {max_depth} reached, returning as-is")
        return obj
    
    if isinstance(obj, str):
        # Check if this string looks like JSON (starts with { or [)
        stripped = obj.strip()
        if stripped and (stripped.startswith('{') or stripped.startswith('[')):
            try:
                parsed = json.loads(stripped)
                # Recursively process the parsed result (it may contain more JSON strings)
                result = _deep_parse_json_strings(parsed, depth + 1, max_depth)
                logger.debug(f"[DEEP_PARSE] Parsed JSON string at depth {depth}: {stripped[:50]}...")
                return result
            except (json.JSONDecodeError, ValueError):
                # Not valid JSON, return as-is
                return obj
        return obj
    
    elif isinstance(obj, dict):
        # Recursively process all values in the dict
        return {k: _deep_parse_json_strings(v, depth + 1, max_depth) for k, v in obj.items()}
    
    elif isinstance(obj, list):
        # Recursively process all items in the list
        return [_deep_parse_json_strings(item, depth + 1, max_depth) for item in obj]
    
    else:
        # For all other types (int, float, bool, None, etc.), return as-is
        return obj


def _convert_to_serializable(obj: Any, depth: int = 0, max_depth: int = 20) -> Any:
    """Convert protobuf and other non-serializable types to JSON-serializable Python types.
    
    GEMINI MAPCOMPOSITE FIX (Dec 2025):
    Gemini API returns proto.marshal.collections.maps.MapComposite objects
    which are not JSON serializable. This function recursively converts them
    to regular Python dicts/lists.
    
    Args:
        obj: The object to convert
        depth: Current recursion depth
        max_depth: Maximum recursion depth to prevent infinite loops
    
    Returns:
        JSON-serializable Python object
    """
    if depth > max_depth:
        logger.warning(f"[SERIALIZABLE] Max depth {max_depth} reached, converting to str")
        return str(obj)
    
    # Handle None
    if obj is None:
        return None
    
    # Handle primitives
    if isinstance(obj, (str, int, float, bool)):
        return obj
    
    # Handle regular dict
    if isinstance(obj, dict):
        return {str(k): _convert_to_serializable(v, depth + 1, max_depth) for k, v in obj.items()}
    
    # Handle regular list/tuple
    if isinstance(obj, (list, tuple)):
        return [_convert_to_serializable(item, depth + 1, max_depth) for item in obj]
    
    # Handle protobuf MapComposite and similar map-like objects
    # Check for proto.marshal types by checking class name (avoid import dependency)
    obj_class_name = obj.__class__.__name__
    obj_module = obj.__class__.__module__ if hasattr(obj.__class__, '__module__') else ''
    
    if 'MapComposite' in obj_class_name or 'proto.marshal' in obj_module:
        # Convert MapComposite to dict
        try:
            return {str(k): _convert_to_serializable(v, depth + 1, max_depth) for k, v in obj.items()}
        except Exception as e:
            logger.warning(f"[SERIALIZABLE] Failed to convert {obj_class_name}: {e}")
            return str(obj)
    
    if 'RepeatedComposite' in obj_class_name or ('proto.marshal' in obj_module and hasattr(obj, '__iter__')):
        # Convert RepeatedComposite to list
        try:
            return [_convert_to_serializable(item, depth + 1, max_depth) for item in obj]
        except Exception as e:
            logger.warning(f"[SERIALIZABLE] Failed to convert {obj_class_name}: {e}")
            return str(obj)
    
    # Handle objects with .items() method (dict-like)
    if hasattr(obj, 'items') and callable(obj.items):
        try:
            return {str(k): _convert_to_serializable(v, depth + 1, max_depth) for k, v in obj.items()}
        except Exception:
            pass
    
    # Handle objects with __iter__ (list-like) but not strings
    if hasattr(obj, '__iter__') and not isinstance(obj, (str, bytes)):
        try:
            return [_convert_to_serializable(item, depth + 1, max_depth) for item in obj]
        except Exception:
            pass
    
    # Fallback: convert to string
    try:
        return str(obj)
    except Exception:
        return f"<non-serializable: {obj_class_name}>"


# Native message types
from modules.llm.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)


@dataclass
class StandardToolCall:
    """Standardized tool call structure compatible with OpenAI tool-call schemas.

    This ensures consistency across all tool call creation and validation.
    """
    name: str  # Tool/function name
    args: Dict[str, Any]  # Arguments as dict
    id: str = field(default_factory=lambda: str(uuid4()))  # Unique ID
    type: str = "function"  # Standard type for OpenAI tool-call compatibility

    def to_dict(self) -> Dict[str, Any]:
        """Convert to standard dict format for AIMessage.tool_calls"""
        return {
            "name": self.name,
            "args": self.args,
            "id": self.id,
            "type": self.type
        }


class ToolCallBuilder:
    """Builder for creating standardized tool calls.

    RESPONSIBILITY CONTRACT:
    - ToolCallBuilder.normalize_tool_call: Handles provider-specific tool_call format differences ONLY
      (OpenAI function nesting, IDs, arguments JSON parsing)
    - utils_json.normalize_action_schema: Handles action payload field name transformations ONLY
      (done.text, write_file.file_path, etc.)

    These are separate concerns and should NOT have overlapping logic.
    """

    @staticmethod
    def normalize_and_correct(tool_call: Any, provider: str = None, llm_content: str = None) -> Dict[str, Any]:
        """Normalize and correct tool call in a single pass.

        This combines format normalization and field corrections for optimal performance.

        Handles:
        - OpenAI format (nested 'function' field)
        - Anthropic format (direct fields)
        - Flat format (direct fields with args)
        - Object-based formats (using getattr)
        - JSON string arguments
        - Field name corrections (file_name → file_path, message → text, etc.)

        Args:
            tool_call: Tool call in any supported format (dict or object)
            provider: Optional provider name for provider-specific handling
            llm_content: Unused, kept for API compatibility

        Returns:
            Normalized and corrected tool call dict with fields: name, args, id, type
        """
        # Step 1: Normalize format (existing logic)
        normalized = ToolCallBuilder.normalize_tool_call(tool_call)

        # Step 2: Apply field corrections immediately
        from agents.task.utils_json import apply_action_field_corrections

        tool_name = normalized['name']
        corrected_args = apply_action_field_corrections(tool_name, normalized['args'])
        normalized['args'] = corrected_args

        return normalized

    @staticmethod
    def normalize_tool_call(tool_call: Any) -> Dict[str, Any]:
        """Normalize a tool call to standard format - FORMAT ONLY.

        SINGULAR RESPONSIBILITY: Handle provider format differences ONLY.
        Does NOT apply field corrections - use normalize_and_correct() for that.

        Handles:
        - OpenAI format (nested 'function' field)
        - Anthropic format (direct fields)
        - Flat format (direct fields with args)
        - Object-based formats (using getattr)
        - JSON string arguments

        Args:
            tool_call: Tool call in any supported format (dict or object)

        Returns:
            Normalized tool call dict with guaranteed fields: name, args, id, type
        """
        try:
            # Initialize with safe defaults
            normalized = {
                "name": "unknown",
                "args": {},
                "id": str(uuid4()),
                "type": "function"
            }

            # Handle object formats (convert to dict first)
            if not isinstance(tool_call, dict):
                # Try to extract fields from object
                tool_dict = {}

                # Check for OpenAI-style nested function
                if hasattr(tool_call, 'function'):
                    func = tool_call.function
                    tool_dict['function'] = {
                        'name': getattr(func, 'name', 'unknown'),
                        'arguments': getattr(func, 'arguments', '{}')
                    }
                    tool_dict['id'] = getattr(tool_call, 'id', str(uuid4()))
                else:
                    # Direct attributes
                    tool_dict['name'] = getattr(tool_call, 'name', 'unknown')
                    tool_dict['args'] = getattr(tool_call, 'args', getattr(tool_call, 'arguments', {}))
                    tool_dict['id'] = getattr(tool_call, 'id', str(uuid4()))
                    tool_dict['type'] = getattr(tool_call, 'type', 'function')

                tool_call = tool_dict

            # Now handle dict format
            if "function" in tool_call and isinstance(tool_call.get("function"), dict):
                # OpenAI format with nested 'function'
                func = tool_call["function"]
                normalized["name"] = func.get("name", "unknown")

                # Parse arguments if they're a JSON string
                args = func.get("arguments", {})
                logger.info(f"[NORMALIZE_TOOL_CALL] {normalized['name']}: raw args type={type(args).__name__}, value={str(args)[:300]}")

                if isinstance(args, str):
                    try:
                        parsed_args = json.loads(args) if args else {}
                        # GROK 4.1 FIX: Deep parse to handle nested JSON strings
                        parsed_args = _deep_parse_json_strings(parsed_args)
                        logger.info(f"[NORMALIZE_TOOL_CALL] {normalized['name']}: parsed args (deep)={parsed_args}")
                        normalized["args"] = parsed_args
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(f"Failed to parse JSON arguments: {e}. Using raw string.")
                        normalized["args"] = {"raw": args}
                else:
                    # Already a dict (native Anthropic/OpenAI path). Convert protobuf
                    # types (Gemini MapComposite) to serializable Python. Do NOT
                    # deep-parse string VALUES here: a legit string arg that merely
                    # looks like JSON (e.g. write_file content='{"k":"v"}') would be
                    # turned into a dict and then fail str-field validation, silently
                    # dropping the action. (Deep-parse stays on the string-args path
                    # above, where the whole blob was JSON — the real Grok case.)
                    normalized["args"] = _convert_to_serializable(args or {})

                normalized["id"] = tool_call.get("id", str(uuid4()))
            else:
                # Direct format (Anthropic, etc.)
                normalized["name"] = tool_call.get("name", "unknown")

                # Handle various argument field names
                args = tool_call.get("args") or tool_call.get("arguments") or tool_call.get("input", {})

                # Parse if string
                if isinstance(args, str):
                    try:
                        parsed_args = json.loads(args) if args else {}
                        # GROK 4.1 FIX: Deep parse to handle nested JSON strings
                        normalized["args"] = _deep_parse_json_strings(parsed_args)
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(f"Failed to parse JSON arguments: {e}. Using raw string.")
                        normalized["args"] = {"raw": args}
                else:
                    # Already a dict (native path). Convert protobuf types to
                    # serializable Python. Do NOT deep-parse string VALUES here — a
                    # legit JSON-looking string arg would be corrupted into a dict/list
                    # and fail str-field validation. (See the OpenAI branch above.)
                    normalized["args"] = _convert_to_serializable(args or {})

                normalized["id"] = tool_call.get("id", str(uuid4()))
                normalized["type"] = tool_call.get("type", "function")

            # Validate the normalized result
            if not normalized["name"] or normalized["name"] == "unknown":
                logger.warning(f"Tool call missing name: {tool_call}")
                # Fall back to a safe default name
                normalized["name"] = "error_unknown_tool"

            # Ensure name is never empty (Anthropic rejects empty names)
            if not normalized["name"] or not normalized["name"].strip():
                logger.error(f"Tool call has empty name, using fallback: {tool_call}")
                normalized["name"] = "error_empty_name"

            # Ensure args is always a dict
            if not isinstance(normalized["args"], dict):
                logger.warning(f"Converting non-dict args to dict: {type(normalized['args'])}")
                normalized["args"] = {"value": normalized["args"]}

            return normalized

        except Exception as e:
            logger.error(f"Failed to normalize tool call: {e}. Input: {tool_call}")
            # Return a safe fallback
            return {
                "name": "error",
                "args": {"error": str(e), "original": str(tool_call)[:200]},
                "id": str(uuid4()),
                "type": "function"
            }



# --- Backward-compat re-exports (P9) -------------------------------------------
# Message-sequence repair/validation moved to tool_message_repair.py; keep these
# importable from here (filters.py, service.py, modules/llm/openai_client.py).
from agents.task.agent.message_manager.tool_message_repair import (  # noqa: E402
    detect_and_remove_duplicate_tool_calls,
    repair_tool_message_pairs,
    validate_tool_message_pairs,
    repair_and_normalize,
)
