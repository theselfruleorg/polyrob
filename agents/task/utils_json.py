"""Centralized JSON extraction utilities for AutoV2.

This module provides consistent JSON extraction from model outputs
with standardized error messages and layered parsing strategies.
"""

import json
import re
import logging
from typing import Any, Dict, Optional, List, Union, Tuple
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# Common field name variations mapping
FIELD_SYNONYMS = {
    'text': ['message', 'msg', 'content', 'value', 'string', 'data'],
    'file_path': ['file_name', 'filename', 'path', 'filepath', 'file'],
    'content': ['text', 'data', 'body', 'contents', 'value'],
    'selector': ['element', 'target', 'locator', 'xpath', 'css'],
    'url': ['link', 'href', 'address', 'uri', 'website'],
    'query': ['search', 'q', 'search_query', 'search_term', 'term'],
    'parent_pattern': ['parent', 'parent_id', 'parent_text', 'under'],
    'pattern': ['text', 'match', 'search', 'regex', 'string'],
}

_CAMEL_BOUNDARY_1 = re.compile(r'(.)([A-Z][a-z]+)')
_CAMEL_BOUNDARY_2 = re.compile(r'([a-z0-9])([A-Z])')


def camel_to_snake(name: str) -> str:
    """Convert camelCase / PascalCase to snake_case; idempotent for snake_case.

    ``filePath`` → ``file_path``, ``maxResults`` → ``max_results``,
    ``userID`` → ``user_id``, ``file_path`` → ``file_path`` (unchanged).
    """
    if not name:
        return name
    s1 = _CAMEL_BOUNDARY_1.sub(r'\1_\2', name)
    return _CAMEL_BOUNDARY_2.sub(r'\1_\2', s1).lower()


def reconcile_field_names_to_model(
    params: Dict[str, Any], valid_fields: set
) -> Dict[str, Any]:
    """Systematic camelCase→snake_case reconciliation against a model's fields (WS-1.3).

    Replaces the hand-maintained per-field whack-a-mole for the *casing* class of
    error: a key is renamed to its snake_case form ONLY when that snake_case form
    is a real field of the target Pydantic param model and the camelCase one is
    not. This is provider-agnostic (any model that emits ``filePath`` for a
    ``file_path`` field is fixed) yet safe for tools whose params are genuinely
    camelCase (e.g. some MCP servers) — there, the snake_case form is NOT a model
    field, so the key is left untouched. Semantic renames (``message``→``text``)
    still go through ``apply_action_field_corrections``.
    """
    if not params or not valid_fields:
        return params
    out: Dict[str, Any] = {}
    for key, value in params.items():
        if key in valid_fields:
            out[key] = value
            continue
        snake = camel_to_snake(key)
        if snake != key and snake in valid_fields and snake not in params:
            out[snake] = value
            logger.debug(f"Reconciled camelCase field '{key}' -> '{snake}'")
        else:
            out[key] = value
    return out


def apply_action_field_corrections(action_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Apply action-specific field corrections with enhanced fuzzy matching.

    This function handles common LLM mistakes by mapping incorrect field names
    to their expected counterparts. It uses a comprehensive mapping table and
    supports fuzzy matching for common variations.
    """
    if not params:
        return params

    corrected = {}

    # Enhanced corrections with more variations
    corrections = {
        # Core action: done (Controller action, not TaskTool)
        'done': {
            'message': 'text', 'result': 'text', 'output': 'text',
            'completion_message': 'text', 'done_text': 'text', 'summary': 'text',
            'final_message': 'text', 'completion': 'text', 'status': 'text'
        },
        # Namespaced task todo actions (TaskTool actions)
        # Legacy names (todo_add, etc.) are handled via action_aliases below
        'task_todo_add': {
            'message': 'text', 'task': 'text', 'todo': 'text', 'item': 'text',
            'description': 'text', 'content': 'text', 'todo_text': 'text',
            'task_text': 'text', 'todo_message': 'text', 'task_message': 'text'
        },
        'task_todo_complete': {
            'index': 'id', 'todo_id': 'id', 'item_id': 'id', 'task_id': 'id'
        },
        'write_file': {
            'file_name': 'file_path', 'filename': 'file_path', 'path': 'file_path',
            'name': 'file_path', 'file': 'file_path', 'filepath': 'file_path',
            'data': 'content', 'text': 'content', 'file_content': 'content',
            'body': 'content', 'contents': 'content', 'file_data': 'content'
        },
        'read_file': {
            'file_name': 'file_path', 'filename': 'file_path', 'path': 'file_path',
            'name': 'file_path', 'file': 'file_path', 'filepath': 'file_path'
        },
        'click_element': {
            'element': 'index', 'target': 'index', 'locator': 'index',
            'element_index': 'index', 'click_target': 'index',
            'selector': 'index',
        },
        'click': {  # Alias for click_element
            'element': 'index', 'target': 'index', 'locator': 'index',
            'element_index': 'index', 'selector': 'index',
        },
        'search_google': {
            'search': 'query', 'q': 'query', 'search_term': 'query', 'term': 'query',
            'search_query': 'query', 'google_query': 'query', 'text': 'query',
            'search_text': 'query', 'keyword': 'query', 'keywords': 'query'
        },
        'navigate': {
            'address': 'url', 'link': 'url', 'website': 'url', 'site': 'url',
            'destination': 'url', 'uri': 'url', 'navigate_to': 'url', 'goto': 'url'
        },
        'input_text': {
            'element': 'index', 'target': 'index', 'element_index': 'index',
            'locator': 'index', 'selector': 'index',
            'input': 'text', 'value': 'text', 'content': 'text',
            'input_value': 'text', 'type_text': 'text', 'string': 'text'
        },
        'type_text': {
            'input': 'text', 'content': 'text', 'value': 'text', 'string': 'text',
            'type_value': 'text', 'input_text': 'text', 'enter_text': 'text'
        }
    }

    # Also handle action name variations (legacy -> canonical)
    action_aliases = {
        'click': 'click_element',
        'goto': 'navigate',
        'go_to': 'navigate',
        'open': 'navigate',
        'type': 'type_text',
        'input': 'type_text',
        'search': 'search_google',
        'google': 'search_google',
        # Todo aliases - map legacy names to namespaced names
        'todo_add': 'task_todo_add',
        'add_todo': 'task_todo_add',
        'create_todo': 'task_todo_add',
        'todo_list': 'task_todo_list',
        'todo_complete': 'task_todo_complete',
        'todo_progress': 'task_todo_progress',
        'todo_next': 'task_todo_next',
        'finish': 'done',
        'complete': 'done',
        'save_file': 'write_file',
        'create_file': 'write_file',
        'open_file': 'read_file',
        'load_file': 'read_file'
    }

    # Extract base name from namespaced action (e.g., "browser_click_element" -> "click_element")
    base_action_name = action_name
    if '_' in action_name:
        parts = action_name.split('_', 1)
        if len(parts) == 2 and (parts[1] in corrections or parts[1] in action_aliases):
            base_action_name = parts[1]
            logger.debug(f"Extracted base action: '{action_name}' -> '{base_action_name}'")

    # Normalize action name if it's an alias
    normalized_action = action_aliases.get(base_action_name.lower(), base_action_name)

    # Try multiple lookup strategies
    action_corrections = (
        corrections.get(action_name, {}) or
        corrections.get(normalized_action, {}) or
        corrections.get(base_action_name, {})
    )

    # Process parameters with case-insensitive matching
    for field, value in params.items():
        field_lower = field.lower()

        # Check if this field needs correction
        correct_field = action_corrections.get(field_lower, field)

        # If no direct match, try to find a close match — but on a '_'-delimited
        # TOKEN boundary, not a raw substring. A substring test against short keys
        # ('q'->'query', 'text'->'content') mis-maps unrelated fields ('quality',
        # 'context') and clobbers the real one. Require the shorter name to be a
        # whole token of the longer, and never overwrite a field already placed.
        if correct_field == field and action_corrections:
            field_tokens = field_lower.split('_')
            for wrong_field, right_field in action_corrections.items():
                wf = wrong_field.lower()
                if wf in field_tokens or field_lower in wf.split('_'):
                    if right_field in corrected:
                        continue  # don't clobber an exact/earlier-corrected field
                    correct_field = right_field
                    break

        corrected[correct_field] = value
        if field != correct_field:
            logger.debug(f"Corrected field: '{field}' -> '{correct_field}' for '{action_name}'")

    return corrected

def preprocess_action_data(action: Dict[str, Any]) -> Tuple[str, Any]:
    """Extract action name and params from action dict.

    Args:
        action: Action dictionary with single key-value pair

    Returns:
        Tuple of (action_name, action_params)
    """
    if not action or not isinstance(action, dict):
        return "unknown", {}

    action_name = list(action.keys())[0] if action else "unknown"
    action_params = action.get(action_name, {})
    return action_name, action_params


def _extract_balanced_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract a balanced JSON object from text using brace counting.
    
    Args:
        text: Text containing a JSON object
        
    Returns:
        Parsed JSON dict or None if extraction fails
    """
    start = text.find('{')
    if start < 0:
        return None
    
    brace_count = 0
    end = start
    for i, char in enumerate(text[start:], start):
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0:
                end = i + 1
                break
    
    if brace_count != 0:
        return None
    
    try:
        json_str = text[start:end]
        return json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_function_args(func_args_str: str) -> Dict[str, Any]:
    """Parse function arguments from a string like 'arg1="val1", arg2=123'.
    
    Handles both quoted strings and unquoted values (int, float, bool, null).
    
    Args:
        func_args_str: Arguments string from function call
        
    Returns:
        Dictionary of parsed arguments
    """
    func_args = {}
    
    if not func_args_str or not func_args_str.strip():
        return func_args
    
    # First match quoted values: key="value" or key='value'
    quoted_pattern = r'(\w+)\s*=\s*["\']([^"\']*)["\']'
    for arg_match in re.finditer(quoted_pattern, func_args_str):
        func_args[arg_match.group(1)] = arg_match.group(2)
    
    # Then match unquoted values: key=123, key=true, key=false, key=null
    unquoted_pattern = r'(\w+)\s*=\s*([0-9]+(?:\.[0-9]+)?|true|false|null)'
    for arg_match in re.finditer(unquoted_pattern, func_args_str, re.IGNORECASE):
        arg_name = arg_match.group(1)
        if arg_name not in func_args:  # Don't overwrite quoted values
            val_str = arg_match.group(2).lower()
            if val_str == 'true':
                func_args[arg_name] = True
            elif val_str == 'false':
                func_args[arg_name] = False
            elif val_str == 'null':
                func_args[arg_name] = None
            elif '.' in arg_match.group(2):
                func_args[arg_name] = float(arg_match.group(2))
            else:
                func_args[arg_name] = int(arg_match.group(2))
    
    return func_args


def _parse_function_calls(func_section: str) -> List[Dict[str, Any]]:
    """Parse all function calls from a Functions: section.
    
    Args:
        func_section: Text after "Functions:" containing function calls
        
    Returns:
        List of action dicts like [{func_name: {args}}]
    """
    func_call_pattern = r'(\w+)\(([^)]*)\)'
    all_actions = []
    
    for func_match in re.finditer(func_call_pattern, func_section):
        func_name = func_match.group(1)
        func_args_str = func_match.group(2)
        func_args = _parse_function_args(func_args_str)
        all_actions.append({func_name: func_args})
        logger.debug(f"Parsed function '{func_name}' with args: {func_args}")
    
    return all_actions


def _build_converted_response(
    all_actions: List[Dict[str, Any]], 
    current_state_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Build a converted response dict from actions and optional current_state.
    
    Args:
        all_actions: List of action dicts
        current_state_data: Optional parsed current_state JSON
        
    Returns:
        Response dict with 'action' and 'current_state' fields
    """
    converted = {"action": all_actions}
    
    if current_state_data:
        if "current_state" in current_state_data:
            converted["current_state"] = current_state_data["current_state"]
        else:
            converted["current_state"] = current_state_data
    else:
        # Create minimal current_state for schema validation
        first_action_name = list(all_actions[0].keys())[0] if all_actions else "unknown"
        converted["current_state"] = {
            "memory": "Action from function call format",
            "evaluation_previous_goal": "Unknown",
            "next_goal": first_action_name
        }
    
    return converted

def extract_json_from_model_output(text: str) -> Dict[str, Any]:
    """Extract JSON from model output with consistent error handling.

    Tries multiple extraction strategies:
    1. Strip code fences and think tags
    2. Try direct JSON parsing
    3. Extract from various block formats
    4. Find JSON-like structures
    5. Validate required keys

    Args:
        text: Model output text potentially containing JSON

    Returns:
        Parsed JSON as dictionary

    Raises:
        ValueError with standardized messages:
        - "Empty response from model" for None/empty input
        - "Could not parse response" for extraction failures
    """
    if not text:
        raise ValueError("Empty response from model")

    # Import config for validation - single source of truth
    from agents.task.robust_parse_config import RobustParseConfig as Config

    # Debug logging - only at DEBUG level to prevent log spam
    logger.debug(f"extract_json_from_model_output input (first 500 chars): {text[:500]}")

    original_text = text

    # FIX 3a: Strip code fences and think tags first
    if Config.STRIP_CODE_FENCES:
        # Remove code fences - properly handle content between them
        text = re.sub(r'```[a-z]*\n?', '', text, flags=re.IGNORECASE)
        text = re.sub(r'```\s*$', '', text)
        # Remove triple quotes
        text = re.sub(r'"""[a-z]*\n?', '', text, flags=re.IGNORECASE)
        text = re.sub(r'"""', '', text)

    if Config.STRIP_THINK_TAGS:
        # Remove think tags and their content
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # Try direct parsing first (after stripping)
    try:
        parsed = json.loads(text.strip())
        # FIX 3d: Validate required keys
        if Config.REQUIRE_SCHEMA_KEYS:
            if Config.validate_json_candidate(json.dumps(parsed)):
                logger.debug(f"Direct JSON parse succeeded with validation")
                return parsed
            # Continue to other strategies if validation fails
            logger.debug(f"Direct JSON parse succeeded but validation failed")
        else:
            logger.debug(f"Direct JSON parse succeeded (no validation)")
            return parsed
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug(f"Direct JSON parse failed: {e}")
        pass

    # T-04: Enhanced pattern detection for function-calling format
    # Try to detect OpenAI function call format first
    function_call_pattern = r'\{\s*"function"\s*:\s*"[^"]+"\s*,\s*"args"\s*:\s*\{[^}]*\}\s*\}'
    func_match = re.search(function_call_pattern, original_text)
    if func_match:
        try:
            func_call = json.loads(func_match.group())
            # Convert to standard action format
            if "function" in func_call and "args" in func_call:
                converted = {
                    "action": [{func_call["function"]: func_call.get("args", {})}]
                }
                # Add a minimal current_state if required
                if Config.REQUIRE_SCHEMA_KEYS:
                    converted["current_state"] = {
                        "page_summary": "Action from function call",
                        "evaluation_previous_goal": "Unknown",
                        "memory": "",
                        "next_goal": func_call["function"]
                    }
                logger.debug(f"Converted OpenAI function-call format to standard action")
                return converted
        except (json.JSONDecodeError, TypeError):
            pass

    # T-05: Handle "Text: {...} Functions: func_name(...)" format from OpenRouter/some models
    # Format: Text: {"current_state": {...}} \n Functions: func_name(arg1="val1", arg2="val2")
    # CRITICAL FIX (Dec 2025): Refactored to use helper functions
    if 'Text:' in original_text and 'Functions:' in original_text:
        try:
            text_idx = original_text.find('Text:')
            func_idx = original_text.find('Functions:')
            if text_idx >= 0 and func_idx > text_idx:
                # Extract JSON from between Text: and Functions:
                json_section = original_text[text_idx + 5:func_idx].strip()
                current_state_data = _extract_balanced_json(json_section)

                # Parse all function calls
                func_section = original_text[func_idx + 10:].strip()
                all_actions = _parse_function_calls(func_section)

                if all_actions:
                    converted = _build_converted_response(all_actions, current_state_data)
                    logger.debug(f"Converted Text/Functions format to {len(all_actions)} action(s)")
                    return converted
        except (json.JSONDecodeError, TypeError, AttributeError, ValueError) as e:
            logger.debug(f"Failed to parse Text/Functions format: {e}")

    # T-05b: Handle "Functions:" only format (without "Text:" prefix)
    # Some models return just "Functions: func_name(args)" without any JSON brain state
    # CRITICAL FIX (Dec 2025): Refactored to use helper functions
    elif 'Functions:' in original_text:
        try:
            func_idx = original_text.find('Functions:')
            func_section = original_text[func_idx + 10:].strip()
            
            # Try to extract JSON from before Functions: section
            json_before = original_text[:func_idx].strip()
            current_state_data = _extract_balanced_json(json_before) if json_before else None
            
            # Parse all function calls
            all_actions = _parse_function_calls(func_section)
            
            if all_actions:
                converted = _build_converted_response(all_actions, current_state_data)
                logger.debug(f"Converted Functions-only format to {len(all_actions)} action(s)")
                return converted
        except Exception as e:
            logger.debug(f"Failed to parse Functions-only format: {e}")

    # FIX 3b: Try extracting from blocks - prefer first valid JSON
    patterns = [
        r'\{[^{}]*"current_state"[^{}]*"action"[^{}]*\}',  # Prioritize JSON with required keys
        r'\{.*?"current_state".*?"action".*?\}',            # Larger JSON with required keys
        r'\{[^{}]*\}',                                       # Simple JSON objects
        r'\{.*?\}',                                          # Any JSON object (non-greedy)
    ]

    for pattern in patterns:
        matches = re.findall(pattern, original_text, re.DOTALL)
        for match in matches:
            try:
                cleaned = match.strip()
                if cleaned:
                    parsed = json.loads(cleaned)
                    # FIX 3d: Validate and return first valid JSON
                    if Config.REQUIRE_SCHEMA_KEYS:
                        if Config.validate_json_candidate(json.dumps(parsed)):
                            logger.debug(f"Successfully extracted JSON with required keys from pattern: {pattern[:30]}...")
                            return parsed
                    else:
                        return parsed
            except (json.JSONDecodeError, TypeError):
                continue

    # Last resort: find anything that looks like JSON
    # Look for content between first { and last }
    start = original_text.find('{')
    end = original_text.rfind('}')

    if start >= 0 and end > start:
        potential_json = original_text[start:end+1]
        try:
            parsed = json.loads(potential_json)
            # FIX 3d: Final validation
            if Config.REQUIRE_SCHEMA_KEYS:
                if Config.validate_json_candidate(json.dumps(parsed)):
                    logger.debug("Successfully extracted JSON from bracket positions")
                    return parsed
                else:
                    # Log what keys are missing for debugging
                    missing_keys = [k for k in Config.REQUIRED_KEYS if k not in parsed]
                    logger.warning(f"JSON found but missing required keys: {missing_keys}")
            else:
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    # FIX 3c: Keep standardized error message
    raise ValueError("Could not parse response")


def validate_json_response(response: Any, required_fields: Optional[list] = None) -> Dict[str, Any]:
    """Validate and extract JSON from a model response.

    Args:
        response: Response object or string from model
        required_fields: Optional list of required field names

    Returns:
        Validated JSON dictionary

    Raises:
        ValueError: If validation fails with standardized message
    """
    # Extract content from response object if needed
    content = response
    if hasattr(response, 'content'):
        content = response.content
    elif hasattr(response, 'text'):
        content = response.text

    # Convert to string if needed
    if not isinstance(content, str):
        content = str(content)

    # Extract JSON
    try:
        result = extract_json_from_model_output(content)
    except ValueError:
        # Re-raise with standardized message
        raise

    # Validate required fields if specified
    if required_fields:
        missing = [f for f in required_fields if f not in result]
        if missing:
            raise ValueError(f"Could not parse response: missing required fields {missing}")

    return result





def normalize_action_schema(data: Union[Dict[str, Any], List, Any]) -> Union[Dict[str, Any], List, Any]:
    """Normalize field names in LLM response to handle provider inconsistencies.

    RESPONSIBILITY CONTRACT:
    - This function ONLY handles action payload field name transformations
    - It does NOT handle tool_call format differences (that's ToolCallBuilder.normalize_tool_call's job)
    - Keep these responsibilities separate to avoid duplication and confusion

    This function centralizes field name normalization logic that was previously
    duplicated in Agent._normalize_field_names. Fixes common issues where LLMs
    use incorrect field names based on training patterns.

    Common fixes:
    - Function-calling format conversion: {"function": "name", "args": {...}} -> {"name": {...}}
    - Field name mappings: done.message -> done.text, todo_add.message -> todo_add.text
    - write_file.file_name -> write_file.file_path

    Args:
        data: Raw data from LLM response (dict, list, or primitive)

    Returns:
        Normalized data with corrected field names
    """
    if isinstance(data, dict):
        normalized_data = {}
        for key, value in data.items():
            # Recursively normalize nested objects
            normalized_value = normalize_action_schema(value)

            # Handle specific field name mappings for actions
            if key == "action" and isinstance(normalized_value, list):
                # Process each action in the list
                fixed_actions = []
                for action in normalized_value:
                    if isinstance(action, dict):
                        # Handle function-calling format {"function": "name", "args": {...}}
                        if "function" in action and "args" in action:
                            # Convert from function-calling format to ActionModel format
                            func_name = action["function"]
                            func_args = action.get("args", {})

                            # Apply action-specific field corrections
                            func_args = apply_action_field_corrections(func_name, func_args)

                            fixed_action = {func_name: func_args}
                            logger.debug(f"Converted function-calling format: {func_name}")
                        else:
                            # Standard format, process normally
                            # Preprocess the action to handle various formats
                            action_name, action_params = preprocess_action_data(action)

                            if isinstance(action_params, dict):
                                # Apply field corrections
                                action_params = apply_action_field_corrections(action_name, action_params)

                            fixed_action = {action_name: action_params}
                        fixed_actions.append(fixed_action)
                    else:
                        # Non-dict action, keep as is
                        fixed_actions.append(action)
                normalized_data[key] = fixed_actions
            else:
                # Regular field, no special processing
                normalized_data[key] = normalized_value

        return normalized_data
    elif isinstance(data, list):
        return [normalize_action_schema(item) for item in data]
    else:
        return data


#: Keys that mark a JSON object as agent brain-state telemetry rather than an
#: arbitrary result.  A ``current_state`` wrapper or >=2 of these ⇒ brain-state.
_BRAIN_STATE_KEYS = frozenset({
    'current_state', 'next_goal', 'evaluation_previous_goal', 'page_summary',
    'memory', 'reasoning', 'macro_goal', 'subgoal', 'immediate_step',
})


def is_brain_state_content(content: Optional[str]) -> bool:
    """True when *content* is (purely) an agent brain-state JSON object.

    POLYROB's system prompt instructs every model to emit its brain state as a
    ``{"current_state": {...}}`` JSON object in the text-content field every
    turn.  That content is internal telemetry, NOT the agent's user-facing voice
    — it must never be streamed to output consumers (CLI / API / WebView), or it
    surfaces as a raw JSON dump on a tool-free planning turn for any streaming
    provider.

    Conservative by design: returns True only when the trimmed content parses as
    a JSON object carrying a ``current_state`` wrapper or >=2 brain keys.
    Genuine prose — and partial token chunks that don't parse — return False and
    stream normally.
    """
    if not content:
        return False
    flat = content.strip()
    if not (flat.startswith('{') and flat.endswith('}')):
        return False
    try:
        obj = json.loads(flat)
    except (ValueError, TypeError):
        return False
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get('current_state'), dict):
        return True
    return sum(1 for k in obj if k in _BRAIN_STATE_KEYS) >= 2


def extract_brain_state_from_json(content: str) -> Dict[str, Any]:
    """Extract brain state from LLM content in JSON format.

    This function handles brain state extraction for native tools mode where
    the LLM provides brain state as JSON in the text content field.

    Supports both formats:
    - Nested: {"current_state": {"memory": "...", ...}}
    - Top-level: {"memory": "...", "next_goal": "...", ...}

    Args:
        content: Text content from LLM response

    Returns:
        Dictionary with brain state fields (memory, evaluation_previous_goal, etc.)

    Raises:
        ValueError: If content cannot be parsed as JSON
    """
    if not content or not content.strip():
        raise ValueError("Empty content - cannot extract brain state")

    # Use existing JSON extraction logic
    brain_json = extract_json_from_model_output(content)

    # Check if brain state is nested under 'current_state' (new format)
    # or at top level (old format)
    if 'current_state' in brain_json and isinstance(brain_json['current_state'], dict):
        # New nested format - extract from current_state
        source = brain_json['current_state']
        logger.debug("Extracting brain state from nested 'current_state' field")
    else:
        # Old top-level format - use directly
        source = brain_json
        logger.debug("Extracting brain state from top-level fields")

    # Map fields to AgentBrain schema
    brain_fields = {
        'page_summary': source.get('page_summary', ''),
        'memory': source.get('memory', ''),
        'evaluation_previous_goal': source.get('evaluation_previous_goal', 'Unknown'),
        'next_goal': source.get('next_goal', ''),
        'reasoning': source.get('reasoning', ''),
        'phase': source.get('phase'),
        'macro_goal': source.get('macro_goal'),
        'subgoal': source.get('subgoal'),
        'immediate_step': source.get('immediate_step')
    }

    logger.debug(f"Extracted brain state: memory={brain_fields.get('memory', '')[:100]}")

    return brain_fields