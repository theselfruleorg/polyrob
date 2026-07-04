"""POLYROB subagents commands — inspect background delegations.

Delegations are session-scoped and in-memory. This command can show:
- General delegation capability info
- Active delegations for a live session (if attached/queried)
- Static info about limits and configuration
"""
from __future__ import annotations

import json
from typing import Optional

import click

from agents.task.constants import TimeoutConfig


@click.group("subagents")
def subagents():
    """Inspect agent delegation and subagent activity."""
    pass


@subagents.command("info")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def subagents_info(as_json: bool):
    """Show delegation capability and limits."""
    # Lazy import to avoid heavy tools import chain
    try:
        from tools.controller.delegation import get_blocked_child_tools
        blocked_tools = sorted(get_blocked_child_tools())
    except Exception:
        # Fallback if tools import fails
        blocked_tools = []

    info = {
        "delegation_enabled": TimeoutConfig.get_sub_agents_enabled(),
        "max_concurrent": TimeoutConfig.get_max_concurrent_sub_agents(),
        "max_depth": TimeoutConfig.get_max_sub_agent_depth(),
        "max_async": TimeoutConfig.get_max_async_sub_agents(),
        "sync_timeout": TimeoutConfig.get_sub_agent_timeout(),
        # The parallel/async (background) delegation path uses its own longer
        # timeout, not the sync one — report the real value.
        "async_timeout": TimeoutConfig.get_parallel_subtasks_timeout(),
        "blocked_tools": blocked_tools,
    }

    if as_json:
        click.echo(json.dumps(info, indent=2))
    else:
        click.echo("Delegation Capability:")
        click.echo(f"  Enabled: {info['delegation_enabled']}")
        click.echo(f"  Max concurrent: {info['max_concurrent']}")
        click.echo(f"  Max depth: {info['max_depth']}")
        click.echo(f"  Max background: {info['max_async']}")
        click.echo(f"  Sync timeout: {info['sync_timeout']}s")
        click.echo(f"  Async timeout: {info['async_timeout']}s")
        if info['blocked_tools']:
            click.echo(f"  Blocked tools: {', '.join(info['blocked_tools'])}")


@subagents.command("list")
@click.option("--session-id", help="Session to inspect (requires live session or persisted data).")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def subagents_list(session_id: Optional[str], as_json: bool):
    """List subagents/delegations.

    Note: Delegations are currently in-memory and session-scoped. Without a live
    session connection, this can only show persisted records (future work).
    """
    if as_json:
        click.echo(json.dumps({
            "supported": False, "delegations": [],
            "reason": "CLI delegation introspection is not yet persisted; "
                      "use /subagents in a live REPL session.",
        }, indent=2))
        return
    if session_id:
        click.echo(click.style("[polyrob] ", fg="yellow") +
                   "Live session introspection requires API/WebView connection")
        click.echo("CLI will support persisted delegation records in a future update")
    else:
        click.echo("Use /subagents in an active REPL session, or specify --session-id for persisted records")


@subagents.command("show")
@click.argument("delegation_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def subagents_show(delegation_id: str, as_json: bool):
    """Show details for a specific delegation."""
    if as_json:
        click.echo(json.dumps({
            "supported": False, "delegation_id": delegation_id,
            "reason": "CLI delegation introspection is not yet persisted; "
                      "use /subagents in a live REPL session.",
        }, indent=2))
        return
    click.echo(click.style("[polyrob] ", fg="yellow") +
               "Live delegation introspection requires API/WebView connection")
    click.echo("CLI will support persisted delegation records in a future update")
