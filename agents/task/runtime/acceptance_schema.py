"""Validate acceptance declarations before any probe runs or goal is persisted."""
from pathlib import PurePosixPath, PureWindowsPath

MAX_CHECKS = 10
CHECK_FIELDS = {
    "artifact_glob": {"type", "pattern", "arg"},
    "http_ok": {"type", "url", "arg"},
    "file_contains": {"type", "path", "arg", "contains", "mode"},
    "artifact": {"type", "id", "name", "arg", "contains", "mode"},
}


def relative_path(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("expected a nonempty workspace-relative path")
    if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
            or ".." in PurePosixPath(value).parts or "\\" in value):
        raise ValueError("path must stay inside the run workspace")
    return value


def validate_acceptance_checks(checks, registered_types):
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise ValueError(f"acceptance_checks must be a list of at most {MAX_CHECKS}; none may be discarded")
    normalized = []
    for index, raw in enumerate(checks):
        if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
            raise ValueError(f"check {index + 1}: malformed check (requires type)")
        check = dict(raw)
        kind = check["type"] = raw["type"].strip()
        if kind not in registered_types:
            raise ValueError(f"unknown check type {kind!r} (fail-closed)")
        if "workspace_dir" in check:
            raise ValueError("workspace_dir belongs to trusted run context, not check declarations")
        fields = CHECK_FIELDS.get(kind)
        if fields is not None and set(check) - fields:
            raise ValueError(f"check {index + 1}: unexpected fields {sorted(set(check) - fields)}")
        key = {"artifact_glob": "pattern", "file_contains": "path", "http_ok": "url"}.get(kind)
        if key:
            value = check.get(key) or check.get("arg")
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{kind}: no {key}")
            if kind != "http_ok":
                relative_path(value)
            else:
                from core.security.http_probe import origin
                origin(value)
        if kind == "artifact":
            if bool(check.get("id")) == bool(check.get("name") or check.get("arg")):
                raise ValueError("artifact: supply exactly one of id or name")
            for field in ("id", "name", "arg"):
                if field in check and (not isinstance(check[field], str) or not check[field].strip()):
                    raise ValueError(f"artifact: {field} must be nonempty text")
        if kind in {"artifact", "file_contains"}:
            contains = check.get("contains", [])
            if isinstance(contains, str):
                contains = [contains]
            if (not isinstance(contains, list) or len(contains) > 100
                    or any(not isinstance(s, str) or not s or len(s) > 10000 for s in contains)):
                raise ValueError(f"{kind}: contains must be bounded nonempty text entries")
            if kind == "file_contains" and not contains:
                raise ValueError("file_contains: no substrings given ('contains' empty)")
            check["contains"] = contains
            if check.get("mode", "all") not in {"any", "all"}:
                raise ValueError(f"{kind}: mode must be all or any")
        normalized.append(check)
    return normalized
