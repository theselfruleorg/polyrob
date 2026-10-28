"""Operator MCP servers live in the operator file, not in the code tree (093 B3, D5).

``config/mcp_config.json`` is a tracked repo file: a server put there needs a
commit, a push and a deploy, and the next deploy overwrites it. The operator's
own servers belong in ``<polyrob_home>/mcp.json`` — the file
``tools/mcp/config.py::load_local_mcp_servers`` already overlays on the repo
defaults (it supports every transport, ``${VAR}`` values and ``"money": true``;
a deploy never touches it).

:func:`migrate_repo_servers` copies each server of the deployed repo file into
that operator file ONCE. Idempotent: a name already in the operator file is
left alone, and every copied name is recorded under :data:`MARKER`, so a server
the operator later deletes from the operator file is not copied back while the
repo file still lists it. An unreadable operator file is never overwritten.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Top-level key in the operator file: the repo server names already copied.
MARKER = "_migrated_from_repo"


def operator_mcp_file() -> Path:
    """``<polyrob_home>/mcp.json`` — the file ``load_local_mcp_servers`` reads."""
    from core.paths import polyrob_home
    return polyrob_home() / "mcp.json"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_private(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mcp.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _target_key(home: Dict[str, Any]) -> str:
    """The key the operator's entries live in: the one that HOLDS entries
    (``{"servers": {}, "mcpServers": {...}}`` → ``mcpServers``), else the one
    present, else ``servers``."""
    for key in ("servers", "mcpServers"):
        if isinstance(home.get(key), dict) and home[key]:
            return key
    return "mcpServers" if "mcpServers" in home and "servers" not in home else "servers"


def migrate_repo_servers(repo_file: Path, home_file: Optional[Path] = None) -> List[str]:
    """Copy the repo file's servers into the operator file once; the names copied."""
    repo = _read_json(Path(repo_file)) if Path(repo_file).exists() else None
    servers = (repo or {}).get("servers") or {}
    if not isinstance(servers, dict) or not servers:
        return []
    home_file = Path(home_file) if home_file is not None else operator_mcp_file()
    if home_file.exists():
        home = _read_json(home_file)
        if home is None:
            logger.warning("MCP operator file %s is unreadable; repo servers not migrated", home_file)
            return []
    else:
        home = {}
    key = _target_key(home)
    present = home.get(key) if isinstance(home.get(key), dict) else {}
    held = set(present)
    for other in ("servers", "mcpServers"):
        if isinstance(home.get(other), dict):
            held |= set(home[other])
    done = set(home.get(MARKER) or [])
    copied = [n for n in servers if n not in held and n not in done]
    newly_marked = [n for n in servers if n not in done]
    if not newly_marked:
        return []
    for name in copied:
        present[name] = servers[name]
    home[key] = present
    home[MARKER] = sorted(done | set(newly_marked))
    _write_private(home_file, home)
    if copied:
        logger.info("MCP: copied repo server(s) %s into the operator file %s", copied, home_file)
    return copied
