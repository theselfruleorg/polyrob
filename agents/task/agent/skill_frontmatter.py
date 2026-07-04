"""Real YAML frontmatter for SKILL.md (agentskills.io). Replaces the legacy flat
`key: value` splitter that could not read a nested `metadata:` block. PyYAML
safe_load = lenient runtime parse; strict authoring rules live in skill_validation.py."""
import json
from typing import Any, Dict, Tuple
import yaml

from .skill_validation import ALLOWED_TOP

FENCE = "---"
_TRUTHY = {"true", "1", "yes", "on"}
# Emission order for the whitelisted top-level fields (metadata is appended
# separately below, after any string-map coercion). ALLOWED_TOP is the single
# source of truth for WHICH fields are allowed (skill_validation.py); this
# tuple is purely presentation order and is asserted to cover the same set so
# the two can never silently drift.
_EMIT_ORDER = ("name", "description", "license", "compatibility", "allowed-tools")
assert set(_EMIT_ORDER) | {"metadata"} == ALLOWED_TOP, "emit order must cover ALLOWED_TOP"

def split_frontmatter(content: str) -> Tuple[str | None, str]:
    if not content:
        return None, content or ""
    text = content.lstrip("﻿")
    if not text.startswith(FENCE):
        return None, text
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == FENCE:
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]).lstrip("\n")
    return None, text  # unterminated fence -> treat as plain body

def _quote_colon_values(yaml_text: str) -> str:
    out = []
    for line in yaml_text.splitlines():
        key, sep, val = line.partition(":")
        v = val.strip()
        if sep and v and ":" in v and v[0] not in "\"'[{|>":
            out.append(f'{key}: "{v.replace(chr(34), chr(39))}"')
        else:
            out.append(line)
    return "\n".join(out)

def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    yaml_text, body = split_frontmatter(content)
    if yaml_text is None:
        return {}, body
    for text in (yaml_text, _quote_colon_values(yaml_text)):
        try:
            meta = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        if isinstance(meta, dict):
            return meta, body
        return {}, body
    return {}, body

def strip_frontmatter(content: str) -> str:
    return parse_frontmatter(content)[1]

def parse_bool(v: Any) -> bool:
    return str(v).strip().lower() in _TRUTHY  # NB: bool("false") is True; this is not.

def encode_triggers(triggers: Dict[str, Any]) -> str:
    return json.dumps(triggers or {}, sort_keys=True, separators=(",", ":"))

def decode_triggers(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    try:
        d = json.loads(v)
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}

def emit_frontmatter(meta: Dict[str, Any]) -> str:
    ordered: Dict[str, Any] = {}
    for k in _EMIT_ORDER:
        val = meta.get(k)
        if val in (None, ""):
            continue
        # agentskills.io: `allowed-tools` is a space-separated string. A list/tuple
        # must be space-joined, not str()'d into a mangled Python repr.
        if k == "allowed-tools" and isinstance(val, (list, tuple)):
            ordered[k] = " ".join(str(x) for x in val)
        else:
            ordered[k] = str(val)
    md = meta.get("metadata") or {}
    if md:
        ordered["metadata"] = {str(k): str(val) for k, val in md.items()}
    text = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=4096).rstrip("\n")
    return f"{FENCE}\n{text}\n{FENCE}\n"
