"""SKILL.md frontmatter (agentskills.io): parse, emit and validate.

PyYAML ``safe_load`` is the lenient runtime parse. :func:`validate_authored` holds
the strict authoring rules, :func:`validate_consumed` the lenient load rules, and
:func:`validate_skill_content` / :func:`validate_skill_id` the write-time checks.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import yaml

# --- size and id limits -------------------------------------------------------
# Task 7: two thresholds, not one flat cap. The old 12000-char hard reject refused
# spec-valid agentskills.io skills (anthropics/skills `docx` is 20084 chars).
MAX_SKILL_FILE_CHARS = 40000      # on-disk ceiling (DoS guard) - the only hard reject
MAX_SKILL_INJECT_CHARS = 20000    # ~5000 tok recommended injected-body size (agentskills.io)
SOFT_INJECT_WARN_CHARS = MAX_SKILL_INJECT_CHARS  # warn-only threshold at injection time
MIN_SKILL_CONTENT_CHARS = 50     # Minimum meaningful content
MAX_SKILL_ID_LENGTH = 50
#: 081 WS-4.3: ``metadata.polyrob-when`` — the skill-index line that says when to
#: load the skill. One line, at most this many characters.
MAX_WHEN_CHARS = 60

# --- validation ---------------------------------------------------------------

ALLOWED_TOP = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
_NAME_STRICT = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")   # ascii kebab, no --, no lead/trail -

@dataclass
class Issue:
    level: str   # "error" | "warn"
    code: str
    msg: str

def validate_authored(meta: dict, dirname: str) -> list[Issue]:
    out: list[Issue] = []
    name = meta.get("name")
    if not name:
        out.append(Issue("error", "missing_name", "name is required"))
    elif not isinstance(name, str):
        out.append(Issue("error", "name_not_string", "name must be a string"))
    else:
        if len(name) > 64:
            out.append(Issue("error", "name_too_long", "name >64 chars"))
        if not _NAME_STRICT.match(name):
            out.append(Issue("error", "name_charset", "name must be ^[a-z0-9]+(-[a-z0-9]+)*$"))
        if name != dirname:
            out.append(Issue("error", "name_dir_mismatch", f"name {name!r} != dir {dirname!r}"))
    desc = meta.get("description")
    if not desc:
        out.append(Issue("error", "missing_description", "description is required"))
    elif not isinstance(desc, str):
        out.append(Issue("error", "description_not_string", "description must be a string"))
    elif len(desc) > 1024:
        out.append(Issue("error", "description_too_long", "description >1024 chars"))
    extra = set(meta.keys()) - ALLOWED_TOP
    if extra:
        out.append(Issue("error", "extra_top_level_field", f"unexpected top-level: {sorted(extra)}"))
    comp = meta.get("compatibility")
    if comp and len(str(comp)) > 500:
        out.append(Issue("error", "compatibility_too_long", "compatibility >500 chars"))
    md = meta.get("metadata")
    if md is not None and (not isinstance(md, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in md.items())):
        out.append(Issue("error", "metadata_not_string_map", "metadata must be string->string"))
    out.extend(Issue("error", "when_line", msg) for msg in when_errors(meta))
    return out

def when_line(meta: Any) -> str:
    """``metadata.polyrob-when`` of a parsed frontmatter, "" when absent."""
    md = meta.get("metadata") if isinstance(meta, dict) else None
    value = md.get("polyrob-when") if isinstance(md, dict) else None
    return value.strip() if isinstance(value, str) else ""


def when_errors(meta: Any) -> List[str]:
    """The ``polyrob-when`` index line, when present, is one line of <= 60 chars."""
    md = meta.get("metadata") if isinstance(meta, dict) else None
    if not isinstance(md, dict) or md.get("polyrob-when") in (None, ""):
        return []
    value = md["polyrob-when"]
    if not isinstance(value, str) or "\n" in value.strip():
        return ["metadata.polyrob-when must be one line of text"]
    if len(value.strip()) > MAX_WHEN_CHARS:
        return [f"metadata.polyrob-when is {len(value.strip())} chars; the skill index line "
                f"is at most {MAX_WHEN_CHARS} (say when to load the skill, e.g. "
                f"'Moving funds between chains (bridge)')"]
    return []


def validate_consumed(meta: dict, dirname: str) -> list[Issue]:
    out: list[Issue] = []
    name = meta.get("name")
    if not meta.get("description"):
        out.append(Issue("error", "missing_description", "description required to load"))   # skip
    if name and not isinstance(name, str):
        out.append(Issue("warn", "name_not_string", "name is not a string (loaded anyway)"))
    elif name and len(name) > 64:
        out.append(Issue("warn", "name_too_long", "name >64 (loaded anyway)"))
    if name and isinstance(name, str) and name != dirname:
        out.append(Issue("warn", "name_dir_mismatch", "name!=dir (loaded anyway)"))
    return out

# --- parse / emit -------------------------------------------------------------
FENCE = "---"
_TRUTHY = {"true", "1", "yes", "on"}
# Emission order for the whitelisted top-level fields (metadata is appended
# separately below, after any string-map coercion). ALLOWED_TOP is the single
# source of truth for WHICH fields are allowed (above); this
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


# --- write-time checks --------------------------------------------------------

@dataclass
class SkillValidationResult:
    """Result of skill validation."""
    skill_id: str
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def validate_skill_content_length(body: str) -> Tuple[bool, str]:
    """The ONE hard-reject window: [MIN_SKILL_CONTENT_CHARS, MAX_SKILL_FILE_CHARS].

    A body between MAX_SKILL_INJECT_CHARS and MAX_SKILL_FILE_CHARS is accepted;
    the injection path warns past MAX_SKILL_INJECT_CHARS, it never rejects.
    """
    n = len(body)
    if n < MIN_SKILL_CONTENT_CHARS:
        return False, f"too short ({n}<{MIN_SKILL_CONTENT_CHARS})"
    if n > MAX_SKILL_FILE_CHARS:
        return False, f"too large ({n}>{MAX_SKILL_FILE_CHARS})"
    return True, ""


def validate_skill_id(skill_id: str) -> Tuple[bool, List[str]]:
    """A writable skill id: lowercase kebab, starts with a letter, <= 50 chars."""
    errors: List[str] = []
    if not skill_id:
        return False, ["Skill ID cannot be empty"]
    if len(skill_id) > MAX_SKILL_ID_LENGTH:
        errors.append(f"Skill ID exceeds max length of {MAX_SKILL_ID_LENGTH}")
    if not re.match(r'^[a-z][a-z0-9-]*$', skill_id):
        errors.append("Skill ID must be lowercase, start with letter, contain only letters, numbers, hyphens")
    if '--' in skill_id:
        errors.append("Skill ID cannot contain consecutive hyphens")
    if skill_id.endswith('-'):
        errors.append("Skill ID cannot end with hyphen")
    return not errors, errors


def validate_skill_content(skill_id: str, content: str) -> SkillValidationResult:
    """Write-time content check: not empty, within the file ceiling, a heading,
    and a ``metadata.polyrob-when`` index line of <= 60 chars when one is given."""
    if not content or not content.strip():
        return SkillValidationResult(skill_id=skill_id, is_valid=False,
                                     errors=["Skill content is empty"])
    errors: List[str] = list(when_errors(parse_frontmatter(content)[0]))
    warnings: List[str] = []
    content_len = len(content)
    if content_len < MIN_SKILL_CONTENT_CHARS:
        warnings.append(f"Skill content very short ({content_len} chars) - may not be useful")
    if content_len > MAX_SKILL_FILE_CHARS:
        errors.append(f"Skill content too large ({content_len} chars, max {MAX_SKILL_FILE_CHARS}) - will bloat prompts")
    # An optional YAML frontmatter block may precede the heading (agentskills.io).
    if not strip_frontmatter(content).strip().startswith('#'):
        warnings.append("Skill should start with a markdown heading")
    estimated_tokens = content_len // 4
    if estimated_tokens > 2000:
        warnings.append(f"Skill uses ~{estimated_tokens} tokens - consider trimming")
    return SkillValidationResult(skill_id=skill_id, is_valid=not errors,
                                 errors=errors, warnings=warnings)
