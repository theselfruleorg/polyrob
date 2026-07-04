import re
from dataclasses import dataclass

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
    return out

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
