"""The owner must be able to add an MCP server from the chat.

Today the only way to give the agent a new MCP server is to edit
`config/mcp_config.json` or `MCP_INSTALL_CATALOG_FILE` on the box. The owner's
standing directive is that he can do anything from the chat, and he is usually
on a phone, so a capability reachable only from a shell is one he does not have.

This is the ONE helper set every owner seat renders (Telegram, REPL, console),
mirroring `core/app_service/owner_ops.py`. It is deliberately thin over
`UserMCPService`, which already owns the security envelope: HTTPS-only, no
internal IPs, stdio refused, credentials encrypted at rest, per-tenant server
cap and rate limit. This module adds no second store and no second policy.
"""
import asyncio
from dataclasses import dataclass
from typing import List, Optional

import pytest


@dataclass
class _Row:
    """The fields `UserMCPServer` exposes that a listing actually renders."""

    server_name: str
    server_url: str
    server_type: str = "http"
    enabled: bool = True
    display_name: Optional[str] = None
    tools_discovered: int = 0
    last_error: Optional[str] = None


class _FakeService:
    """Stands in for UserMCPService with its real return shapes."""

    def __init__(self, rows: Optional[List[_Row]] = None, add_error: Optional[str] = None):
        self.rows = list(rows or [])
        self.add_error = add_error
        self.added = []
        self.removed = []

    async def get_user_servers(self, user_id, enabled_only=False):
        return list(self.rows)

    async def add_server(self, user_id, server_name, server_url, **kw):
        from tools.mcp.user_mcp_service import AddServerResult
        if self.add_error:
            return AddServerResult(success=False, error=self.add_error)
        self.added.append((user_id, server_name, server_url, kw))
        row = _Row(server_name, server_url)
        self.rows.append(row)
        return AddServerResult(success=True, server=row, ready=True)

    async def delete_server(self, user_id, server_name):
        self.removed.append((user_id, server_name))
        before = len(self.rows)
        self.rows = [r for r in self.rows if r.server_name != server_name]
        return len(self.rows) != before


def _reply(args, *, service, user_id="owner1"):
    from core.mcp_admin import mcp_reply
    return mcp_reply(user_id, args, service=service)


# --- add ------------------------------------------------------------------

def test_add_persists_the_server_and_says_so():
    svc = _FakeService()
    out = _reply(["add", "aave", "https://mcp.aave.com"], service=svc)

    assert svc.added and svc.added[0][1] == "aave"
    assert svc.added[0][2] == "https://mcp.aave.com"
    assert "aave" in out


def test_add_reports_the_validator_refusal_verbatim():
    """A refusal must name what was wrong, not just fail."""
    svc = _FakeService(add_error="Invalid URL: only HTTPS is allowed")
    out = _reply(["add", "evil", "http://10.0.0.1/mcp"], service=svc)

    assert "HTTPS" in out
    assert not svc.added


def test_add_refuses_a_stdio_style_command_without_calling_the_store():
    """stdio is a persisted arbitrary exec on the owner's box. The store already
    refuses it; the chat seat must refuse it EARLY and say why, rather than
    letting a command line look like a URL typo."""
    svc = _FakeService()
    out = _reply(["add", "sketchy", "npx", "-y", "some-package"], service=svc)

    assert not svc.added
    assert "http" in out.lower()


def test_add_needs_both_an_id_and_a_url():
    svc = _FakeService()
    out = _reply(["add", "aave"], service=svc)
    assert not svc.added
    assert "usage" in out.lower()


# --- list -----------------------------------------------------------------

def test_list_shows_saved_servers():
    svc = _FakeService([_Row("aave", "https://mcp.aave.com")])
    out = _reply(["list"], service=svc)
    assert "aave" in out and "mcp.aave.com" in out


def test_list_is_honest_when_nothing_is_saved():
    out = _reply([], service=_FakeService())
    assert "no mcp servers" in out.lower()


# --- remove ---------------------------------------------------------------

def test_remove_drops_the_row():
    svc = _FakeService([_Row("aave", "https://mcp.aave.com")])
    out = _reply(["remove", "aave"], service=svc)
    assert svc.removed == [("owner1", "aave")]
    assert "aave" in out


# --- honest unavailability -------------------------------------------------

def test_no_store_says_so_instead_of_crashing():
    """MCP_ENABLED off, or a boot with no database: there is nowhere to save.
    That must read as 'the store is not running', never as a silent success or
    a stack trace."""
    out = _reply(["add", "aave", "https://mcp.aave.com"], service=None)
    assert "not available" in out.lower() or "mcp_enabled" in out.lower()


def test_anonymous_caller_is_refused():
    svc = _FakeService()
    out = _reply(["add", "aave", "https://mcp.aave.com"], service=svc, user_id="")
    assert not svc.added
    assert "owner" in out.lower()
