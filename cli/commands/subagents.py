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
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


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


def _delegations(session_id=None, delegation_id=None):
    from cli.delegation_receipts import read_delegations
    from core.identity import resolve_identity
    from agents.task.agent.autonomy_state import default_autonomy_state_db
    return read_delegations(default_autonomy_state_db(), resolve_identity(), session_id, delegation_id)


@subagents.command("list")
@click.option("--session-id", help="Filter durable receipts by full session ID.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def subagents_list(session_id: Optional[str], as_json: bool):
    """List persisted background delegation receipts, newest first."""
    rows = _delegations(session_id)
    if as_json:
        click.echo(json.dumps({"supported": True, "delegations": rows}, indent=2))
    elif not rows:
        click.echo("No persisted background delegations. Live synchronous children are visible through /subagents.")
    else:
        for row in rows:
            click.echo(f"{row['delegation_id']}  {row['status']}  session={row['session_id']}  {row.get('goal') or ''}")


@subagents.command("show")
@click.argument("delegation_id")
@click.option("--session-id", help="Disambiguate a delegation ID reused in multiple sessions.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def subagents_show(delegation_id: str, as_json: bool, session_id=None):
    """Show a persisted background delegation and its result."""
    rows = _delegations(session_id, delegation_id)
    if not rows:
        raise click.ClickException("Delegation not found. Use polyrob subagents list.")
    if len(rows) > 1:
        raise click.ClickException("Delegation ID is ambiguous; specify --session-id: " + ", ".join(r["session_id"] for r in rows))
    if as_json:
        click.echo(json.dumps(rows[0], indent=2))
    else:
        for key, value in rows[0].items():
            click.echo(f"{key}: {value}")
