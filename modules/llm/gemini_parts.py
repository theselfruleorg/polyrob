"""Pure readers of the Gemini wire: response parts, finish reasons, tool rows, schemas.

A proto-plus ``Part`` exposes EVERY oneof member as an attribute, so
``hasattr(part, "function_call")`` is True for a plain text part too (its
``function_call`` is an empty message with ``name == ""``). Presence must be asked of
the oneof via ``in``.
"""
from typing import Any, Dict, List, Optional, Tuple

from core.exceptions import LLMRefusalError, LLMResponseError
from core.stop_reason import normalize_stop

#: A candidate that stopped with one of these holds no usable answer.
_STOPPED = frozenset({
    "SAFETY", "RECITATION", "LANGUAGE", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    "IMAGE_SAFETY", "MALFORMED_FUNCTION_CALL",
})


def part_is_function_call(part) -> bool:
    """True only when the Part's ``function_call`` oneof member is SET.

    A duck-typed double without ``__contains__`` falls back to "the attribute
    exists and is not None".
    """
    try:
        return "function_call" in part
    except TypeError:
        return getattr(part, "function_call", None) is not None


def enum_name(value: Any) -> Optional[str]:
    """The name of a proto enum value, or None for an unset or unknown value."""
    name = getattr(value, "name", None)
    if not isinstance(name, str):
        name = value if isinstance(value, str) else None
    return None if not name or name.endswith("UNSPECIFIED") else name


def read_candidate(response: Any, model: Optional[str] = None) -> Tuple[str, List[Any]]:
    """(text, function-call parts) of the first candidate. A prompt or a candidate that
    the provider stopped raises a typed error that names the reason. The SDK's
    ``response.text`` raises ``ValueError`` on such a response, so it is not read
    when the response has candidates."""
    blocked = enum_name(getattr(getattr(response, "prompt_feedback", None), "block_reason", None))
    if blocked:
        raise LLMRefusalError(f"Gemini: the prompt was blocked for model {model} (block_reason {blocked})",
                              category=blocked, kind="filtered")
    candidates = getattr(response, "candidates", None)
    if not isinstance(candidates, (list, tuple)) and not hasattr(candidates, "__getitem__"):
        candidates = None
    if not candidates:
        text = getattr(response, "text", None)
        return (text if isinstance(text, str) else ""), []
    candidate = candidates[0]
    reason = enum_name(getattr(candidate, "finish_reason", None))
    parts = getattr(getattr(candidate, "content", None), "parts", None) or []
    function_parts = [p for p in parts if part_is_function_call(p)]
    text = "".join(p.text for p in parts
                   if not part_is_function_call(p) and isinstance(getattr(p, "text", None), str))
    if reason in _STOPPED and normalize_stop(reason) == "filtered":
        raise LLMRefusalError(f"Gemini: finish_reason {reason} stopped model {model}",
                              category=reason, kind="filtered")
    if reason in _STOPPED:
        raise LLMResponseError(f"Gemini: finish_reason {reason} stopped model {model}")
    return text, function_parts


def function_call_args(function_call: Any) -> Dict[str, Any]:
    """The args of a FunctionCall as a dict. A proto Struct has ``items``."""
    raw = getattr(function_call, "args", None)
    return {k: v for k, v in raw.items()} if raw and hasattr(raw, "items") else {}


def tool_call_fields(call: Dict[str, Any]) -> Tuple[Optional[str], Any]:
    """(name, args) of a tool call in the flat or the OpenAI shape."""
    function = call.get("function") or {}
    name = call.get("name") or function.get("name")
    args = call.get("args") or call.get("arguments") or function.get("arguments") or {}
    return name, args


def tool_names_by_id(messages: List[Dict[str, Any]]) -> Dict[str, str]:
    """``tool_call_id`` to function name, from the assistant rows. A FunctionResponse must
    name its function, and a tool row holds only the id."""
    names: Dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                name = tool_call_fields(call)[0]
                if name:
                    names[call["id"]] = name
    return names


def wire_system(system: Optional[Any], messages: List[Dict[str, Any]]) -> Optional[str]:
    """The ``system_instruction`` of a request: an explicit ``system`` wins, else the first
    system row, which is where the agent builds it."""
    if isinstance(system, str) and system:
        return system
    for message in messages:
        if message.get("role") == "system" and message.get("content"):
            content = message["content"]
            return content if isinstance(content, str) else str(content)
    return None


def _nullable_branch(schema: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """The one wire branch of a JSON schema, and whether null is allowed. The pinned SDK's
    ``Schema`` has no union: ``anyOf`` keeps its first non-null branch."""
    nullable = False
    branches = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(branches, list) and branches:
        concrete = [b for b in branches if isinstance(b, dict) and b.get("type") != "null"]
        nullable = len(concrete) < len(branches)
        inherited = {k: v for k, v in schema.items() if k not in ("anyOf", "oneOf")}
        schema = {**(concrete[0] if concrete else {"type": "string"}), **inherited}
    kind = schema.get("type")
    if isinstance(kind, list):
        named = [k for k in kind if k != "null"]
        nullable = nullable or len(named) < len(kind)
        schema = {**schema, "type": named[0] if named else "string"}
    return schema, nullable


def json_schema_to_glm(glm: Any, schema: Dict[str, Any]) -> Any:
    """A JSON schema dict as a ``glm.Schema``, recursively."""
    type_map = {
        "object": glm.Type.OBJECT, "string": glm.Type.STRING, "integer": glm.Type.INTEGER,
        "number": glm.Type.NUMBER, "boolean": glm.Type.BOOLEAN, "array": glm.Type.ARRAY,
    }
    schema, nullable = _nullable_branch(schema)
    fields: Dict[str, Any] = {}
    if "type" in schema:
        fields["type"] = type_map.get(schema["type"], glm.Type.STRING)
    if nullable:
        fields["nullable"] = True
    if "description" in schema:
        fields["description"] = schema["description"]
    if "properties" in schema:
        fields["properties"] = {k: json_schema_to_glm(glm, v) for k, v in schema["properties"].items()}
    if "required" in schema:
        fields["required"] = schema["required"]
    if "items" in schema:
        fields["items"] = json_schema_to_glm(glm, schema["items"])
    if "enum" in schema:
        fields["enum"] = schema["enum"]
    return glm.Schema(**fields)
