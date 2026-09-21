"""Product-facing tool catalog commands.

``status`` answers the question an operator actually asks — "can I use this
here" — which has TWO halves: the tool's own flag, and whether the process they
are standing in can register it at all. C27: this showed only the first, so a
server-only tool read ``enabled`` on a box whose CLI container has never been
able to build it. The CLI availability half is derived from the ONE seam
(``core.bootstrap.cli_unavailable_tools``, over ``_CLI_REGISTERABLE_TOOLS``),
never a second list.
"""

from __future__ import annotations

import functools
import json

import click

from core.tool_catalog import build_tool_catalog, find_tool, permission_catalog


@functools.lru_cache(maxsize=1)
def _catalog() -> tuple:
    """The catalog, built ONCE per process.

    C73: every verb here called ``build_tool_catalog()`` afresh, and
    ``export-catalog --no-json`` built it TWICE in one invocation — it probes
    every tool's config and services, so the cost is real and the two copies
    could disagree with each other inside a single command.
    """
    return tuple(build_tool_catalog())


def _cli_availability(tool_ids) -> dict:
    """``{tool_id: None | reason}`` — ``None`` means the CLI can register it.

    A lookup fault is NOT "available": it comes back as an explicit unknown, so
    the row says so instead of claiming a capability nobody checked.
    """
    try:
        from core.bootstrap import cli_unavailable_tools
        missing = set(cli_unavailable_tools(list(tool_ids)))
    except Exception as exc:
        reason = f"unknown ({type(exc).__name__}: {exc})"
        return {t: reason for t in tool_ids}
    return {t: (None if t not in missing
                else "not registrable in the CLI container (needs the server)")
            for t in tool_ids}


#: The run/toolset surface uses short ids (`browser`) that the catalog registers
#: under a fuller id (`browser_manager`), and ``cli_unavailable_tools`` keys on
#: the run-surface id. Ask it about the id the operator would actually type.
_CATALOG_TO_RUN_ID = {"browser_manager": "browser"}


@click.group("tools")
def tools():
    """Inspect tool catalog, status, and permissions."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@tools.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def tools_list(as_json: bool):
    """List known tools from the catalog."""
    entries = _catalog()
    if as_json:
        click.echo(json.dumps([entry.to_dict() for entry in entries], indent=2, sort_keys=True))
        return
    _print_tool_table(entries)


@tools.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def tools_status(as_json: bool):
    """Show each tool's flag status AND whether this CLI can register it."""
    entries = _catalog()
    avail = _cli_availability([_CATALOG_TO_RUN_ID.get(e.id, e.id) for e in entries])
    if as_json:
        out = []
        for entry in entries:
            row = entry.to_dict()
            row["cli_unavailable_reason"] = avail.get(
                _CATALOG_TO_RUN_ID.get(entry.id, entry.id))
            out.append(row)
        click.echo(json.dumps(out, indent=2, sort_keys=True))
        return
    _print_tool_table(entries, include_reason=True, availability=avail)


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
    run_id = _CATALOG_TO_RUN_ID.get(getattr(entry, "id", ""), getattr(entry, "id", ""))
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
    cli_reason = _cli_availability([run_id]).get(run_id)
    click.echo(f"  cli: {'available' if cli_reason is None else cli_reason}")
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
    catalog = _catalog()
    if as_json:
        click.echo(json.dumps([entry.to_dict() for entry in catalog],
                              indent=2, sort_keys=True))
        return
    _print_tool_table(catalog, include_reason=True)


def _print_tool_table(entries, *, include_reason: bool = False,
                      availability: "dict | None" = None) -> None:
    click.echo(f"{'Tool':<18} {'Category':<12} {'Status':<10} Permissions")
    click.echo("-" * 78)
    for entry in entries:
        status = "enabled" if entry.enabled else "disabled"
        permissions = ", ".join(entry.permissions) or "-"
        line = f"{entry.id:<18} {entry.category:<12} {status:<10} {permissions}"
        if include_reason and entry.disabled_reason:
            line += f" ({entry.disabled_reason})"
        if availability is not None:
            reason = availability.get(_CATALOG_TO_RUN_ID.get(entry.id, entry.id))
            if reason is not None:
                line += click.style(f"  [cli: unavailable — {reason}]", fg="yellow")
        click.echo(line)
