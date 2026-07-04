"""Product-facing tool catalog commands."""

from __future__ import annotations

import json

import click

from core.tool_catalog import build_tool_catalog, find_tool, permission_catalog


@click.group("tools")
def tools():
    """Inspect tool catalog, status, and permissions."""


@tools.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def tools_list(as_json: bool):
    """List known tools from the catalog."""
    entries = build_tool_catalog()
    if as_json:
        click.echo(json.dumps([entry.to_dict() for entry in entries], indent=2, sort_keys=True))
        return
    _print_tool_table(entries)


@tools.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def tools_status(as_json: bool):
    """Show enabled/disabled status and reasons."""
    entries = build_tool_catalog()
    if as_json:
        click.echo(json.dumps([entry.to_dict() for entry in entries], indent=2, sort_keys=True))
        return
    _print_tool_table(entries, include_reason=True)


# The run/toolset surface uses short ids (e.g. `browser`) that the catalog registers
# under a fuller id (`browser_manager`); accept the run-surface id so `tools show
# browser` (from `run --tools browser`) resolves instead of "unknown tool".
_TOOL_ID_ALIASES = {"browser": "browser_manager"}


@tools.command("show")
@click.argument("tool_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def tools_show(tool_id: str, as_json: bool):
    """Show details for one tool."""
    entry = find_tool(tool_id) or find_tool(_TOOL_ID_ALIASES.get(tool_id, ""))
    if entry is None:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"unknown tool: {tool_id}")
        raise SystemExit(1)
    if as_json:
        click.echo(json.dumps(entry.to_dict(), indent=2, sort_keys=True))
        return
    click.echo(f"{entry.id} ({entry.category})")
    click.echo(f"  status: {'enabled' if entry.enabled else 'disabled'}")
    if entry.disabled_reason:
        click.echo(f"  reason: {entry.disabled_reason}")
    click.echo(f"  model: {entry.model_description}")
    click.echo(f"  help: {entry.human_description}")
    click.echo(f"  permissions: {', '.join(entry.permissions) or '-'}")
    click.echo(f"  required config: {', '.join(entry.required_config) or '-'}")
    click.echo(f"  required services: {', '.join(entry.required_services) or '-'}")
    click.echo(f"  risk: cost={entry.cost_risk}, security={entry.security_risk}")


@tools.command("permissions")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def tools_permissions(as_json: bool):
    """List permission classes and mapped tools."""
    permissions = permission_catalog()
    if as_json:
        click.echo(json.dumps(permissions, indent=2, sort_keys=True))
        return
    for permission, tool_ids in permissions.items():
        click.echo(f"{permission:<18} {', '.join(tool_ids)}")


@tools.command("export-catalog")
@click.option("--json/--no-json", "as_json", default=True,
              help="Machine-readable JSON (default); --no-json prints a table.")
def tools_export_catalog(as_json: bool):
    """Export the complete tool catalog."""
    entries = [entry.to_dict() for entry in build_tool_catalog()]
    if as_json:
        click.echo(json.dumps(entries, indent=2, sort_keys=True))
        return
    _print_tool_table(build_tool_catalog(), include_reason=True)


def _print_tool_table(entries, *, include_reason: bool = False) -> None:
    click.echo(f"{'Tool':<18} {'Category':<12} {'Status':<10} Permissions")
    click.echo("-" * 78)
    for entry in entries:
        status = "enabled" if entry.enabled else "disabled"
        permissions = ", ".join(entry.permissions) or "-"
        line = f"{entry.id:<18} {entry.category:<12} {status:<10} {permissions}"
        if include_reason and entry.disabled_reason:
            line += f" ({entry.disabled_reason})"
        click.echo(line)
