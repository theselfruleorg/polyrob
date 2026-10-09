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


# --- 062: the two writes the read-only seat was missing ----------------------

def _toggle(tool_id: str, on: bool, is_global: bool, confirm: bool) -> None:
    """Write a tool's enablement flag through the ONE config writer.

    Refusals are explicit: a tool with no single resolvable flag, and a money /
    high-impact tool, both say why and name the owner path instead of writing
    something the capability gate would overrule anyway.
    """
    from core.tool_capabilities import ids_with, is_classified
    from core.tool_catalog import enable_flag_for

    # The capability table is the full registrable set (~36 ids); the product
    # catalog is the smaller descriptor-backed view, so it is the wrong gate.
    if not is_classified(tool_id):
        raise click.ClickException(
            f"unknown tool '{tool_id}' — `polyrob tools list` shows the catalog")

    # A MONEY tool is an explicit owner grant, never a convenience toggle: the
    # flag is only half of it (the spend lane, the caps and the approval queue
    # are the other half), so this verb refuses and names the deliberate path.
    if on and tool_id in ids_with("money"):
        raise click.ClickException(
            f"'{tool_id}' spends money. Turning it on is a deliberate owner grant, "
            "not a toggle — the spend caps and the approval lane decide the rest.\n"
            f"  polyrob config set {enable_flag_for(tool_id) or '<FLAG>'} true --global\n"
            "Read docs/guide/payments.md before you do.")

    flag = enable_flag_for(tool_id)
    if not flag:
        raise click.ClickException(
            f"'{tool_id}' has no single env flag that turns it on — it is gated by "
            "an installed extra, a credential, or a posture. `polyrob doctor` "
            "names what it needs.")

    # High-impact is NOT a refusal — the owner enabling a tool on their own box
    # is the normal case. It is a WARNING, because the flag is not the whole
    # gate: a delegated, forged or correspondent-tainted turn still cannot
    # reach it.
    if on and tool_id in ids_with("high_impact"):
        click.echo(f"note: '{tool_id}' is high-impact. Enabling it does not give it "
                   "to a sub-agent, a self-wake turn, or a correspondent-tainted "
                   "session — those gates are separate and stay closed.")

    from cli.commands.config import set_cmd
    ctx = click.get_current_context()
    ctx.invoke(set_cmd, key=flag, value=("true" if on else "false"),
               is_global=is_global, project_scope=False, user_id=None,
               confirm=confirm, force=False, home_dir_opt=None)
    click.echo(f"{tool_id}: {flag}={'true' if on else 'false'}")
    click.echo("  Restart running sessions — tool flags are read at session start.")


@tools.command("enable")
@click.argument("tool_id")
@click.option("--global", "--home", "is_global", is_flag=True, default=True, flag_value=True,
              help="Write to ~/.polyrob/.env (the default).")
@click.option("--confirm", is_flag=True, default=False,
              help="Pass through to `config set` for flags that require it.")
def tools_enable(tool_id: str, is_global: bool, confirm: bool):
    """Turn a tool on for future sessions."""
    _toggle(tool_id, True, is_global, confirm)


@tools.command("disable")
@click.argument("tool_id")
@click.option("--global", "--home", "is_global", is_flag=True, default=True, flag_value=True,
              help="Write to ~/.polyrob/.env (the default).")
@click.option("--confirm", is_flag=True, default=False,
              help="Pass through to `config set` for flags that require it.")
def tools_disable(tool_id: str, is_global: bool, confirm: bool):
    """Turn a tool off for future sessions."""
    _toggle(tool_id, False, is_global, confirm)
