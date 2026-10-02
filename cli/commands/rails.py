"""``polyrob rails`` — standing work from the terminal (036 §4.1/§4.4).

The same verbs as ``/rail`` on every chat seat (``surfaces/telegram/rail_ops.py``
is the ONE implementation and renderer), plus the two a file needs:

    polyrob rails export > my-rails.yaml     # the old streams.yaml schema
    polyrob rails import my-rails.yaml       # tools[] in the file -> PENDING grants

The terminal on the box is an owner seat: the store and tenant resolve through
``cli/_admin_home.admin_data_dir`` and ``core.admin_data_home.admin_owner_principal``,
the deployed-home rule every owner verb uses.
"""
from __future__ import annotations

from typing import Tuple

import click


@click.group("rails", invoke_without_command=True)
@click.pass_context
def rails(ctx):
    """Standing work: rails (an objective with a schedule) and their grants."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    if ctx.invoked_subcommand is None:
        click.echo(_run(["list"]))


def _seat():
    from cli._admin_home import admin_data_dir
    from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
    try:
        return admin_owner_principal(), admin_data_dir(write=True)
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))


def _run(words) -> str:
    from surfaces.telegram.rail_ops import rail_reply
    user_id, data_dir = _seat()
    return rail_reply(user_id, data_dir, list(words), seat="cli")


@rails.command("list")
def rails_list():
    """ON / OFF / UNDECLARED."""
    click.echo(_run(["list"]))


@rails.command("show")
@click.argument("name")
def rails_show(name: str):
    """One rail: schedule, next seed, legs, grants (who + when), last seeds."""
    click.echo(_run(["show", name]))


@rails.command("templates")
def rails_templates():
    """The inert offers /rail new can start from."""
    click.echo(_run(["templates"]))


@rails.command("new", context_settings={"ignore_unknown_options": True})
@click.argument("template")
@click.argument("slots", nargs=-1)
def rails_new(template: str, slots: Tuple[str, ...]):
    """Create a rail from a template: polyrob rails new topic-watch topic="AI agents"."""
    click.echo(_run(["new", template, *slots]))


@rails.command("on")
@click.argument("name")
def rails_on(name: str):
    click.echo(_run(["on", name]))


@rails.command("off")
@click.argument("name")
def rails_off(name: str):
    click.echo(_run(["off", name]))


@rails.command("drop")
@click.argument("name")
@click.option("--cancel-live", is_flag=True, help="Also cancel the rail's live legs.")
def rails_drop(name: str, cancel_live: bool):
    """Drop a rail (live legs finish unless --cancel-live)."""
    click.echo(_run(["drop", name] + (["--cancel-live"] if cancel_live else [])))


@rails.command("edit")
@click.argument("name")
@click.argument("field")
@click.argument("value", nargs=-1, required=True)
def rails_edit(name: str, field: str, value: Tuple[str, ...]):
    """Edit body / body.<n> / schedule / max / priority / title."""
    click.echo(_run(["edit", name, field, " ".join(value)]))


@rails.command("grant")
@click.argument("name")
@click.argument("tool")
@click.option("--yes", is_flag=True, help="Confirm (otherwise the grant is only described).")
def rails_grant(name: str, tool: str, yes: bool):
    """Grant a gated tool to every future leg of a rail (asks first)."""
    click.echo(_run(["grant", name, tool] + (["confirm"] if yes else [])))


@rails.command("revoke")
@click.argument("name")
@click.argument("tool")
def rails_revoke(name: str, tool: str):
    """Revoke a grant. Immediate."""
    click.echo(_run(["revoke", name, tool]))


@rails.command("grants")
def rails_grants():
    click.echo(_run(["grants"]))


@rails.command("export")
def rails_export():
    """Print every rail as YAML (the streams.yaml schema)."""
    import yaml

    from agents.task.goals.rails import export_manifest
    from surfaces.telegram.rail_ops import _board
    user_id, data_dir = _seat()
    click.echo(yaml.safe_dump(export_manifest(_board(data_dir), user_id), sort_keys=False,
                              allow_unicode=True))


@rails.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def rails_import(path: str):
    """Create rails from a manifest; tools in the file become PENDING grants."""
    import os

    import yaml

    from agents.task.goals.rails import RailError, import_manifest
    from surfaces.telegram.rail_ops import _board
    resolved = os.path.realpath(path)
    # The old manifest's rule, kept for the import path: an agent-writable
    # session tree is never a source of standing work.
    for frag in (os.sep + "sessions" + os.sep, os.sep + "workspace" + os.sep):
        if frag in resolved + os.sep:
            raise click.ClickException(f"refusing a file inside a session workspace: {resolved}")
    try:
        with open(resolved, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except yaml.YAMLError as e:
        raise click.ClickException(f"{resolved}: invalid YAML: {e}")
    user_id, data_dir = _seat()
    try:
        out = import_manifest(_board(data_dir), user_id, doc, granted_by=f"{user_id}@cli")
    except RailError as e:
        raise click.ClickException(str(e))
    click.echo(f"created: {', '.join(out['created']) or '—'}")
    click.echo(f"kept (already there): {', '.join(out['kept']) or '—'}")
    if out["pending_grants"]:
        click.echo("PENDING grants (nothing applied — confirm each with "
                   "`polyrob rails grant <rail> <tool> --yes`):")
        for item in out["pending_grants"]:
            click.echo(f"  {item}")
