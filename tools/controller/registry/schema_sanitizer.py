"""Sanitize emitted tool JSON schemas for broad LLM-backend compatibility (UP-10 2.5).

Ported from Reference (`tools/schema_sanitizer.py`, the reference agent) and adapted
to POLYROB's four emitted tool-list shapes. POLYROB already has the narrower per-provider
`fix_openai_schema`/`fix_anthropic_schema` (`agents/task/utils.py`) plus the coarse
drop-the-whole-tool `TOOL_SCHEMA_ERROR_POLICY=DROP_TOOL`. This module is the additive
*central seam*: it walks the FINAL emitted tools list (one place,
`Registry.get_all_actions_for_provider`) and fixes known-hostile constructs that those
narrower fixers miss, turning "drop the tool" into "fix the field".

Known-hostile constructs fixed (all on a deep copy, conservative — only shapes the
backend couldn't use anyway):

* bare ``{"type": "object"}`` with no ``properties`` (inject ``properties: {}``) and
  bare-string schema values like ``"object"``.
* ``"type": ["string", "null"]`` array unions → single ``type`` + ``nullable: true``.
* ``anyOf``/``oneOf`` nullable unions → collapse to the non-null branch (Anthropic /
  Kimi reject the null branch at the top of ``input_schema``).
* ``$ref`` + sibling keywords (``default``) → strip the sibling (Fireworks-hosted Kimi /
  draft-07-strict backends reject siblings beside ``$ref``).
* top-level combinators (``allOf``/``anyOf``/``oneOf``/``enum``/``not``) stripped from
  the params root (some strict backends reject them at the outermost level).
* prune ``required`` entries not present in ``properties``.

Two *reactive* helpers (`strip_pattern_and_format`, `strip_slash_enum`) are ported for
parity but **left unwired** — POLYROB has no llama.cpp / xAI-Responses 400-recovery path
today. They are cheap to keep and document the dormant capability.

Gating: the whole pass is behind ``TOOL_SCHEMA_SANITIZE`` (default **on**; ``=false``
restores the exact pre-port emitted bytes). It runs BEFORE the cache write and is
composed downstream of ``DROP_TOOL`` (sanitize first → far fewer tools dropped).
"""

from __future__ import annotations

import copy
import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node-walker primitives (ported verbatim from Reference schema_sanitizer.py)
# ---------------------------------------------------------------------------

# Sibling keywords strict JSON Schema validators reject alongside ``$ref``.
_REF_FORBIDDEN_SIBLINGS = frozenset({"default"})


def _strip_ref_siblings(node: Any) -> Any:
    """Drop forbidden sibling keywords from nodes that carry ``$ref``.

    Fireworks (and other draft-07-strict backends) fail tool requests with a
    "keyword(s) ['default'] not allowed at the same level as $ref" error. Nullable-union
    collapse and MCP ingestion can leave ``default`` on a ``$ref`` node; strip it
    recursively.
    """
    if isinstance(node, list):
        return [_strip_ref_siblings(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {key: _strip_ref_siblings(value) for key, value in node.items()}
    if "$ref" in out:
        for key in _REF_FORBIDDEN_SIBLINGS:
            if key in out:
                out.pop(key, None)
    return out


_TOP_LEVEL_FORBIDDEN_KEYS = ("allOf", "anyOf", "oneOf", "enum", "not")


def _strip_top_level_combinators(params: dict, *, path: str = "<tool>") -> dict:
    """Drop combinator keywords from the top level of a function-parameters schema.

    Some strict backends reject ``oneOf``/``anyOf``/``allOf``/``enum``/``not`` at the
    outermost parameters object. These are typically conditional required-field hints;
    removing them at the top level discards the hint but not which argument *values* are
    valid (the tool handler always re-validates). Only the *top* level is stripped;
    combinators nested inside a property's schema are preserved.
    """
    if not isinstance(params, dict):
        return params
    out = dict(params)
    for key in _TOP_LEVEL_FORBIDDEN_KEYS:
        if key in out:
            logger.debug(
                "schema_sanitizer[%s]: stripped top-level %r combinator "
                "from tool parameters (strict-backend compat)",
                path, key,
            )
            out.pop(key, None)
    return out


def strip_nullable_unions(schema: Any, *, keep_nullable_hint: bool = True) -> Any:
    """Collapse ``anyOf``/``oneOf`` nullable unions to the non-null branch.

    MCP / Pydantic optional fields commonly arrive as
    ``{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}``. Anthropic's
    tool input-schema validator rejects the null branch; optionality is already carried
    by the parent object's ``required`` array, so collapse to the single non-null variant.
    Outer-node metadata (title/description/default/examples) is carried over. With
    ``keep_nullable_hint`` the replacement keeps ``nullable: true``.
    """
    if isinstance(schema, list):
        return [strip_nullable_unions(item, keep_nullable_hint=keep_nullable_hint) for item in schema]
    if not isinstance(schema, dict):
        return schema

    stripped = {
        k: strip_nullable_unions(v, keep_nullable_hint=keep_nullable_hint)
        for k, v in schema.items()
    }
    for key in ("anyOf", "oneOf"):
        variants = stripped.get(key)
        if not isinstance(variants, list):
            continue
        non_null = [
            item for item in variants
            if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        # Only collapse when we actually dropped a null branch AND exactly one non-null
        # branch survives (otherwise the union is meaningful and we leave it alone).
        if len(non_null) == 1 and len(non_null) != len(variants):
            replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
            if keep_nullable_hint:
                replacement.setdefault("nullable", True)
            for meta_key in ("title", "description", "default", "examples"):
                if meta_key in stripped and meta_key not in replacement:
                    # ``default`` is illegal alongside ``$ref`` on strict backends.
                    if meta_key == "default" and "$ref" in replacement:
                        continue
                    replacement[meta_key] = stripped[meta_key]
            return strip_nullable_unions(replacement, keep_nullable_hint=keep_nullable_hint)
    return stripped


def _sanitize_node(node: Any, path: str) -> Any:
    """Recursively sanitize a JSON-Schema fragment.

    - Replaces bare-string schema values ("object", "string", ...) with ``{"type": …}``.
    - Injects ``properties: {}`` into object-typed nodes missing it.
    - Normalizes ``type: [X, "null"]`` arrays to single ``type: X`` (keeps ``nullable``).
    - Recurses into ``properties``, ``items``, ``additionalProperties``, ``anyOf``,
      ``oneOf``, ``allOf``, and ``$defs`` / ``definitions``.
    """
    if isinstance(node, str):
        if node in {"object", "string", "number", "integer", "boolean", "array", "null"}:
            logger.debug(
                "schema_sanitizer[%s]: replacing bare-string schema %r with {'type': %r}",
                path, node, node,
            )
            return {"type": node} if node != "object" else {"type": "object", "properties": {}}
        logger.debug(
            "schema_sanitizer[%s]: replacing non-schema string %r with empty object schema",
            path, node,
        )
        return {"type": "object", "properties": {}}

    if isinstance(node, list):
        return [_sanitize_node(item, f"{path}[{i}]") for i, item in enumerate(node)]

    if not isinstance(node, dict):
        return node

    out: dict = {}
    for key, value in node.items():
        if key == "type" and isinstance(value, list):
            non_null = [t for t in value if t != "null"]
            if len(non_null) == 1 and isinstance(non_null[0], str):
                out["type"] = non_null[0]
                if "null" in value:
                    out.setdefault("nullable", True)
                continue
            first_str = next((t for t in value if isinstance(t, str) and t != "null"), None)
            if first_str:
                out["type"] = first_str
                continue
            out["type"] = "object"
            continue

        if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
            out[key] = {
                sub_k: _sanitize_node(sub_v, f"{path}.{key}.{sub_k}")
                for sub_k, sub_v in value.items()
            }
        elif key in {"items", "additionalProperties"}:
            if isinstance(value, bool):
                out[key] = value
            else:
                out[key] = _sanitize_node(value, f"{path}.{key}")
        elif key in {"anyOf", "oneOf", "allOf"} and isinstance(value, list):
            out[key] = [_sanitize_node(item, f"{path}.{key}[{i}]") for i, item in enumerate(value)]
        elif key in {"required", "enum", "examples", "default", "const", "example"}:
            # Non-schema sibling keywords carrying DATA values, not sub-schemas.
            # Recursing would mis-interpret literal strings ("path") or a dict/list
            # default/const/example as a bare-string/nested schema and corrupt the
            # actual parameter default. Pass through unchanged.
            out[key] = copy.deepcopy(value) if isinstance(value, (list, dict)) else value
        else:
            out[key] = _sanitize_node(value, f"{path}.{key}") if isinstance(value, (dict, list)) else value

    if out.get("type") == "object" and not isinstance(out.get("properties"), dict):
        out["properties"] = {}

    if out.get("type") == "object" and isinstance(out.get("required"), list):
        props = out.get("properties") or {}
        valid = [r for r in out["required"] if isinstance(r, str) and r in props]
        if not valid:
            out.pop("required", None)
        elif len(valid) != len(out["required"]):
            out["required"] = valid

    return out


def _sanitize_params_schema(params: Any, *, path: str = "<params>") -> dict:
    """Run the full sanitize pipeline over a single tool's *parameter* schema.

    Equivalent of Reference' per-tool `_sanitize_single_tool` inner body, but operating on
    a raw params dict (POLYROB wraps params in four different shapes — see
    `sanitize_emitted_tools`). Always returns an object-typed schema with `properties`.
    """
    if not isinstance(params, dict):
        return {"type": "object", "properties": {}}

    out = _sanitize_node(params, path=path)
    if not isinstance(out, dict):
        return {"type": "object", "properties": {}}
    if out.get("type") != "object":
        out["type"] = "object"
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}

    out = strip_nullable_unions(out, keep_nullable_hint=True)
    out = _strip_top_level_combinators(out, path=path)
    out = _strip_ref_siblings(out)
    return out


# ---------------------------------------------------------------------------
# POLYROB-specific format-aware entry point
# ---------------------------------------------------------------------------

def sanitize_emitted_tools(tools: Any, provider: Optional[str] = None) -> Any:
    """Sanitize the inner parameter schema of every emitted tool, format-agnostic.

    POLYROB's `generate_tools_list` returns one of four shapes; this dispatches on shape
    (robust to provider-string drift) and sanitizes the inner schema on a deep copy:

    - **OpenAI** (openai/deepseek/openrouter/nvidia):
      ``[{"type":"function","function":{"parameters": {...}}}]``
    - **Anthropic**: ``[{"name","description","input_schema": {...}}]``
    - **Gemini**: ``[{"function_declarations":[{"name","description","parameters": {...}}]}]``
    - **JSON-fallback** (groq/fireworks/default): a single ``{"type":"object",...}`` dict

    The original input is never mutated. ``provider`` is accepted for logging/signature
    parity but shape-detection drives the dispatch.
    """
    if tools is None:
        return tools

    # JSON-fallback: a single object schema (not a list).
    if isinstance(tools, dict):
        return _sanitize_params_schema(copy.deepcopy(tools), path=f"<{provider or 'json'}>")

    if not isinstance(tools, list):
        return tools

    out: List[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        t = copy.deepcopy(tool)

        # Gemini: one wrapper dict carrying many declarations.
        decls = t.get("function_declarations")
        if isinstance(decls, list):
            for decl in decls:
                if isinstance(decl, dict):
                    decl["parameters"] = _sanitize_params_schema(
                        decl.get("parameters"), path=f"<gemini>.{decl.get('name', '?')}"
                    )
            out.append(t)
            continue

        # OpenAI: {"function": {"parameters": {...}}}.
        fn = t.get("function")
        if isinstance(fn, dict):
            fn["parameters"] = _sanitize_params_schema(
                fn.get("parameters"), path=f"<openai>.{fn.get('name', '?')}"
            )
            out.append(t)
            continue

        # Anthropic: {"input_schema": {...}}.
        if isinstance(t.get("input_schema"), dict):
            t["input_schema"] = _sanitize_params_schema(
                t["input_schema"], path=f"<anthropic>.{t.get('name', '?')}"
            )
            out.append(t)
            continue

        # Unknown list-entry shape — leave as-is (do no harm).
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Reactive helpers — ported for parity, intentionally UNWIRED (no 400-recovery
# path in POLYROB today). Kept so a future llama.cpp / xAI-Responses backend can wire
# them on a backend-400 without re-porting.
# ---------------------------------------------------------------------------

_STRIP_ON_RECOVERY_KEYS = frozenset({"pattern", "format"})


def strip_pattern_and_format(tools: list) -> tuple:
    """Strip ``pattern``/``format`` keywords (llama.cpp grammar-parse 400 recovery).

    DORMANT — no caller in POLYROB. Mutates ``tools`` in place; returns ``(tools, count)``.
    """
    if not tools:
        return tools, 0

    stripped = 0

    def _walk(node: Any) -> None:
        nonlocal stripped
        if isinstance(node, dict):
            is_schema_node = "type" in node or "anyOf" in node or "oneOf" in node or "allOf" in node
            for key in list(node.keys()):
                if is_schema_node and key in _STRIP_ON_RECOVERY_KEYS:
                    node.pop(key, None)
                    stripped += 1
                    continue
                _walk(node[key])
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("parameters"), dict):
            _walk(fn["parameters"])
            continue
        params = tool.get("parameters")
        if isinstance(params, dict):
            _walk(params)

    if stripped:
        logger.info(
            "schema_sanitizer: stripped %d pattern/format keyword(s) (llama.cpp recovery)",
            stripped,
        )
    return tools, stripped


def strip_slash_enum(tools: list) -> tuple:
    """Strip ``enum`` keywords whose string values contain ``/`` (xAI Responses recovery).

    DORMANT — no caller in POLYROB. Mutates ``tools`` in place; returns ``(tools, count)``.
    """
    if not tools:
        return tools, 0

    stripped = 0

    def _walk(node: Any) -> None:
        nonlocal stripped
        if isinstance(node, dict):
            enum_val = node.get("enum")
            if isinstance(enum_val, list) and any(
                isinstance(v, str) and "/" in v for v in enum_val
            ):
                node.pop("enum", None)
                stripped += 1
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("parameters"), dict):
            _walk(fn["parameters"])
            continue
        params = tool.get("parameters")
        if isinstance(params, dict):
            _walk(params)

    if stripped:
        logger.info(
            "schema_sanitizer: stripped %d enum keyword(s) containing '/' (xAI recovery)",
            stripped,
        )
    return tools, stripped
