"""Env-file read/upsert primitives (018 P1).

Promoted from ``cli/commands/config.py::_upsert_env``/``_read_env_file`` so the
core config service can write env flags without importing upward into the cli
tier (the cli module now delegates here). Semantics preserved exactly:
whitespace-normalized key matching (a hand-edited ``KEY = value`` line is
updated in place, never duplicated), 0600 on secure writes, comment lines kept.
"""
from pathlib import Path


def upsert_env_var(path: Path, key: str, value: str, *, secure: bool = True) -> None:
    """Insert or update ``KEY=value`` in *path*, preserving other lines."""
    lines = path.read_text().splitlines() if path.exists() else []
    index = {ln.split("=", 1)[0].strip(): i for i, ln in enumerate(lines) if "=" in ln}
    key = key.strip()
    line = f"{key}={value}"
    if key in index:
        lines[index[key]] = line
    else:
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    if secure:
        path.chmod(0o600)


def read_env_file(path: Path) -> dict:
    """Parse ``KEY=value`` lines (comments/blank skipped); {} when absent."""
    result: dict = {}
    if path.exists():
        for ln in path.read_text().splitlines():
            stripped = ln.strip()
            if "=" in stripped and not stripped.startswith("#"):
                k, v = stripped.split("=", 1)
                result[k.strip()] = v.strip()
    return result
