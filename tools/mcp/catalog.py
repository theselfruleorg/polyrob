"""Curated MCP install catalog (P0-C / Tier-3a).

A small allowlist of vetted MCP servers the agent may install at runtime, mirroring
Hermes's ``optional-mcps/``. Only ids in the builtin allowlist (optionally extended by
``MCP_INSTALL_ALLOWLIST``) can be installed; an arbitrary agent-supplied server config
is never installed directly. Each entry is a NAMED, reviewed config — the agent picks an
id, it does not hand-write the command line. No action closures — ``from __future__`` safe.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional


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


class MCPCatalog:
    """Resolves catalog entries + enforces the install allowlist."""

    def __init__(self, entries: Optional[Dict[str, CatalogEntry]] = None) -> None:
        self._entries = dict(entries if entries is not None else _BUILTIN)

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
