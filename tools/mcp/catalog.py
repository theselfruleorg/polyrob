"""Curated MCP install catalog (P0-C / Tier-3a).

A small allowlist of vetted MCP servers the agent may install at runtime, mirroring
Hermes's ``optional-mcps/``. Only ids with a NAMED, reviewed catalog entry can be
installed; an arbitrary agent-supplied server config is never installed directly —
the agent picks an id, it does not hand-write the command line.

Operators extend the catalog with a REVIEWED JSON file via
``MCP_INSTALL_CATALOG_FILE`` (T3-03): ``{"<id>": {"description": ..., "transport":
"sse|http|stdio", "url": ..., "command": [...], "trust": ...}}``. (The bare
``MCP_INSTALL_ALLOWLIST`` env can allow an id, but an id without an entry has
nothing to install — the file is the real operator seam.)
No action closures — ``from __future__`` safe.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogEntry:
    server_id: str
    description: str
    transport: str            # "sse" | "http" | "stdio"
    url: Optional[str] = None
    command: Optional[List[str]] = None
    trust: str = "official"   # official | trusted | community


# Builtin, reviewed entries — network-transport only (no arbitrary local stdio exec in
# the builtin set); operators extend via MCP_INSTALL_ALLOWLIST.
_BUILTIN: Dict[str, CatalogEntry] = {
    "github": CatalogEntry(
        server_id="github",
        description="GitHub MCP server (issues/PRs) over HTTP",
        transport="http",
        url="https://api.githubcopilot.com/mcp/",
        trust="official",
    ),
    "context7": CatalogEntry(
        server_id="context7",
        description="Context7 docs MCP over SSE",
        transport="sse",
        url="https://mcp.context7.com/sse",
        trust="trusted",
    ),
}


def _load_file_entries() -> Dict[str, CatalogEntry]:
    """T3-03: operator-reviewed extra entries from ``MCP_INSTALL_CATALOG_FILE``.

    Fail-open to {} — a broken file must never break MCP loading; it only means
    no extra entries. Malformed individual entries are skipped with a warning.
    """
    path = (os.getenv("MCP_INSTALL_CATALOG_FILE") or "").strip()
    if not path:
        return {}
    try:
        import json
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("MCP_INSTALL_CATALOG_FILE unreadable (%s): %s", path, e)
        return {}
    out: Dict[str, CatalogEntry] = {}
    if not isinstance(raw, dict):
        logger.warning("MCP_INSTALL_CATALOG_FILE must be a JSON object of id -> entry")
        return {}
    for sid, e in raw.items():
        try:
            out[str(sid)] = CatalogEntry(
                server_id=str(sid),
                description=str(e.get("description", "")),
                transport=str(e.get("transport", "sse")),
                url=e.get("url"),
                command=list(e["command"]) if e.get("command") else None,
                trust=str(e.get("trust", "community")),
            )
        except Exception as ent_err:
            logger.warning("catalog entry %r skipped: %s", sid, ent_err)
    return out


class MCPCatalog:
    """Resolves catalog entries + enforces the install allowlist."""

    def __init__(self, entries: Optional[Dict[str, CatalogEntry]] = None) -> None:
        if entries is not None:
            self._entries = dict(entries)
        else:
            # Builtins + the operator's reviewed file entries (file wins on id clash
            # so an operator can pin/override a builtin deliberately).
            self._entries = {**_BUILTIN, **_load_file_entries()}

    def get(self, server_id: str) -> Optional[CatalogEntry]:
        return self._entries.get(server_id)

    def ids(self) -> List[str]:
        return sorted(self._entries)

    def allowlist(self) -> set:
        """Builtin/registered ids ∪ MCP_INSTALL_ALLOWLIST (comma list)."""
        allowed = set(self._entries)
        raw = os.getenv("MCP_INSTALL_ALLOWLIST", "")
        allowed |= {t.strip() for t in raw.split(",") if t.strip()}
        return allowed

    def is_allowed(self, server_id: str) -> bool:
        return server_id in self.allowlist()
