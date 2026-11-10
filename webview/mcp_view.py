"""Agent › Abilities: an absent operator MCP file on a LOCAL console is empty (113 W5, ST-1).

``tools/mcp/inventory.py`` names "no operator file at … in the console process"
as an error, because on a server the console may run under another home than
the agent and the absence then proves nothing. On a ``local`` console the
console and the agent share one home, so the absence IS the answer: nothing was
configured. A fresh install showed "One list could not be read" on Abilities for
that note alone. An operator file that exists and cannot be parsed stays an
error on every posture (an unreadable store is not an empty one).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def local_absent_file_is_empty(section: dict, container) -> dict:
    """*section* (``mcp_inventory``'s answer) with the absent-file note removed
    when the posture is ``local`` and the operator file does not exist."""
    error = section.get("error") if isinstance(section, dict) else None
    if not error:
        return section
    try:
        from webview import webgate
        if webgate.posture() != "local":
            return section
        from tools.mcp.inventory import operator_servers
        from tools.mcp.operator_home import operator_mcp_file
        if operator_mcp_file().exists():
            return section
        _servers, note = operator_servers(container)
    except Exception:
        logger.debug("mcp view: could not check the operator file", exc_info=True)
        return section
    if not note:
        return section
    rest = [part for part in str(error).split("; ") if part and part != note]
    return dict(section, error="; ".join(rest) or None)
