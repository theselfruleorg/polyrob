"""JSON-schema parameter coercion engine for MCP tool arguments.

Pure module-level functions extracted from MCPTool so they can be unit-tested
independently and reused without instantiating the full MCPTool.

Public API:
  coerce_arguments(schema, arguments, tool_name, *, logger=None)
      -> (converted: dict, errors: list[str])
  enhance_schema_with_date_hints(schema)
      -> enhanced_schema: dict

No circular imports: this module MUST NOT import tools.mcp.mcp_tool.
"""

import copy
import logging
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def coerce_arguments(
    schema: Dict[str, Any],
    arguments: Dict[str, Any],
    tool_name: str,
    *,
    logger: Optional[logging.Logger] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Validate and auto-convert *arguments* against a JSON-schema *schema*.

    Args:
        schema:     JSON Schema dict for the tool's parameters (``input_schema``).
        arguments:  Raw argument dict to validate/coerce.
        tool_name:  Tool name used only in error message strings.
        logger:     Optional logger; falls back to the module logger.

    Returns:
        A ``(converted, errors)`` tuple — same contract as the original
        ``MCPTool._validate_and_convert_parameters``.
    """
    log = logger or _log

    # Enhance schema with date conversion hints before validation
    enhanced_schema = enhance_schema_with_date_hints(schema)

    properties = enhanced_schema.get("properties", {})
    required = enhanced_schema.get("required", [])
    converted: Dict[str, Any] = {}
    errors: List[str] = []

    # Defensive guards: a malformed/hostile schema or non-dict arguments must not
    # raise (TypeError/AttributeError) out of coercion — return errors instead.
    if not isinstance(arguments, dict):
        return {}, [f"[{tool_name}] Arguments must be an object, got {type(arguments).__name__}"]
    if not isinstance(properties, dict):
        properties = {}
    if not isinstance(required, (list, tuple)):
        required = []

    # Check required parameters
    for req_param in required:
        if req_param not in arguments:
            errors.append(f"[{tool_name}] Missing required parameter: {req_param}")

    # Validate and convert each parameter
    for param_name, param_value in arguments.items():
        if param_name not in properties:
            # Allow extra parameters (some tools accept them)
            converted[param_name] = param_value
            continue

        param_schema = properties[param_name]
        param_type = param_schema.get("type")

        if param_type == "integer":
            converted_value, error = _convert_to_integer(
                param_value, param_schema, param_name, log
            )
            if error:
                errors.append(error)
            else:
                converted[param_name] = converted_value

        elif param_type == "string":
            if param_value is None:
                converted[param_name] = None  # preserve explicit null for optional params
            elif isinstance(param_value, str):
                converted[param_name] = param_value
            elif isinstance(param_value, (int, float, bool)):
                converted[param_name] = str(param_value)
            else:
                # dict/list → str() would yield Python-repr garbage ("{'a': 1}")
                errors.append(
                    f"{param_name}: Expected string, got {type(param_value).__name__}"
                )

        elif param_type == "boolean":
            converted_bool, error = _convert_to_boolean(param_value, param_name)
            if error:
                errors.append(error)
            else:
                converted[param_name] = converted_bool

        elif param_type == "array":
            if not isinstance(param_value, list):
                errors.append(
                    f"{param_name}: Expected array, got {type(param_value).__name__}"
                )
            else:
                converted[param_name] = param_value

        elif param_type == "object":
            if not isinstance(param_value, dict):
                errors.append(
                    f"{param_name}: Expected object, got {type(param_value).__name__}"
                )
            else:
                converted[param_name] = param_value

        else:
            # Pass through unknown types
            converted[param_name] = param_value

    return converted, errors


# ---------------------------------------------------------------------------
# Schema enrichment helper
# ---------------------------------------------------------------------------

def enhance_schema_with_date_hints(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Add date-hint descriptions to date-like integer parameters in *schema*.

    Returns a deep-copy — the original schema is never mutated.
    """
    enhanced = copy.deepcopy(schema)
    properties = enhanced.get("properties", {})

    # Keywords that indicate date/timestamp parameters
    DATE_KEYWORDS = [
        "date", "time", "timestamp", "from", "to", "start", "end",
        "created", "updated", "modified", "published",
    ]

    for prop_name, prop_schema in properties.items():
        # Only enhance integer parameters that look like dates
        if prop_schema.get("type") == "integer" and any(
            keyword in prop_name.lower() for keyword in DATE_KEYWORDS
        ):
            original_desc = prop_schema.get("description", "")
            hint = "(Accepts date strings like '2025-11-02' - auto-converted to Unix timestamp)"

            if original_desc:
                if "auto-convert" not in original_desc.lower():
                    prop_schema["description"] = f"{original_desc} {hint}"
            else:
                prop_schema["description"] = hint

            if "examples" not in prop_schema:
                prop_schema["examples"] = ["2025-11-02", "2025-11-02T14:30:00Z"]

    return enhanced


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _try_date_to_timestamp(
    date_string: str,
    logger: logging.Logger,
) -> Optional[int]:
    """Try to convert *date_string* to a Unix timestamp integer.

    Returns the timestamp on success, or ``None`` if conversion is impossible.
    """
    try:
        from utils.time_utils import parse_date_to_timestamp, timestamp_to_date

        timestamp = parse_date_to_timestamp(date_string)
        logger.info(
            f"✅ Auto-converted date string '{date_string}' → {timestamp} "
            f"({timestamp_to_date(timestamp)})"
        )
        return timestamp
    except (ValueError, ImportError) as e:
        logger.debug(f"Date conversion failed for '{date_string}': {e}")
        return None


def _convert_to_boolean(
    value: Any,
    param_name: str,
) -> Tuple[Optional[bool], Optional[str]]:
    """Convert *value* to a bool by VALUE, not Python truthiness.

    ``bool("false")`` / ``bool("0")`` are both True in Python, so the old
    ``bool(param_value)`` silently corrupted string-encoded booleans (which LLMs
    routinely emit). Returns ``(bool, None)`` on success or ``(None, error)``.
    """
    if isinstance(value, bool):
        return value, None
    if isinstance(value, (int, float)):
        return bool(value), None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True, None
        if v in ("false", "0", "no", "off", ""):
            return False, None
        return None, f"{param_name}: Cannot interpret '{value}' as boolean"
    if value is None:
        return None, f"{param_name}: Expected boolean, got null"
    return None, f"{param_name}: Cannot convert {type(value).__name__} to boolean"


def _convert_to_integer(
    value: Any,
    schema: Dict[str, Any],
    param_name: str,
    logger: logging.Logger,
) -> Tuple[Optional[int], Optional[str]]:
    """Convert *value* to an integer, respecting schema constraints.

    Returns ``(int_value, None)`` on success or ``(None, error_msg)`` on failure.
    """
    if isinstance(value, int):
        int_value = value

    elif isinstance(value, str):
        logger.info(
            f"🔍 _convert_to_integer: Converting string '{value}' for param '{param_name}'"
        )

        # Try date-string conversion first, then plain int parse
        int_value = _try_date_to_timestamp(value, logger)

        if int_value is None:
            logger.info("   Date conversion failed, trying integer parse")
            try:
                int_value = int(value)
            except ValueError:
                hint = ""
                if any(
                    kw in param_name.lower()
                    for kw in ["date", "time", "timestamp", "from", "to", "start", "end"]
                ):
                    hint = (
                        " TIP: For dates, use format 'YYYY-MM-DD' or"
                        " 'YYYY-MM-DDTHH:MM:SSZ' (auto-converts to timestamp)."
                    )
                return None, f"{param_name}: Cannot convert '{value}' to integer.{hint}"
        else:
            logger.info(f"   ✅ Date conversion succeeded: '{value}' → {int_value}")

    elif isinstance(value, float):
        int_value = int(value)

    else:
        return None, f"{param_name}: Cannot convert {type(value).__name__} to integer"

    # Validate constraints
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")

    if minimum is not None and int_value < minimum:
        hint = ""
        if minimum > 1_000_000_000:  # Likely a Unix timestamp
            try:
                from utils.time_utils import timestamp_to_date

                min_date = timestamp_to_date(minimum)
                hint = (
                    f" (minimum {minimum} = {min_date},"
                    " use YYYY-MM-DD format and it will auto-convert)"
                )
            except Exception:
                hint = f" (minimum {minimum} appears to be a Unix timestamp)"

        return None, f"{param_name}: Value {int_value} < minimum {minimum}{hint}"

    if maximum is not None and int_value > maximum:
        return None, f"{param_name}: Value {int_value} > maximum {maximum}"

    return int_value, None
