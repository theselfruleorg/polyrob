"""`/mcp` must be reachable, owner-gated and helped.

Giving the agent a new MCP server was a box-side file edit — `mcp_config.json`
or `MCP_INSTALL_CATALOG_FILE`. The owner is usually on a phone, so that is a
capability he does not have. A verb that is handled but not ROUTABLE is dead
code (`/halt` shipped that way), so this pins every list agreeing.
"""
import pytest


def test_the_verb_is_routable():
    from core.surfaces.dispatcher import _COMMANDS
    assert "/mcp" in _COMMANDS


def test_the_verb_is_owner_gated():
    from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS
    assert "/mcp" in _OWNER_ADMIN_COMMANDS


def test_the_verb_is_in_the_help_ssot():
    from surfaces.telegram.harness import help_commands
    assert "mcp" in {c for c, _ in help_commands()}


def test_a_non_owner_is_refused():
    from surfaces.telegram import owner_ops
    assert "Only the owner" in owner_ops.mcp_reply(None, ["list"])


def test_the_bare_verb_lists_rather_than_erroring(monkeypatch):
    seen = {}

    def _fake(user_id, args, **kw):
        seen["args"] = list(args)
        return "listing"

    monkeypatch.setattr("core.mcp_admin.mcp_reply", _fake)
    from surfaces.telegram import owner_ops
    assert owner_ops.mcp_reply("rob", []) == "listing"
    assert seen["args"] == []


def test_add_reaches_the_shared_helper(monkeypatch):
    seen = {}

    def _fake(user_id, args, **kw):
        seen["call"] = (user_id, list(args))
        return "added"

    monkeypatch.setattr("core.mcp_admin.mcp_reply", _fake)
    from surfaces.telegram import owner_ops
    out = owner_ops.mcp_reply("rob", ["add", "aave", "https://mcp.aave.com"])
    assert out == "added"
    assert seen["call"] == ("rob", ["add", "aave", "https://mcp.aave.com"])
