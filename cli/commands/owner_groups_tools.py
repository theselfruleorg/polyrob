"""101: `polyrob owner groups tools|audit`, attached to the `groups` group."""
import click

from cli.commands._bootstrap import ensure_env_loaded
from cli.commands.owner import _group_container, _group_owner_uid, as_root_option, groups


@groups.command("tools")
@click.argument("surface")
@click.argument("chat_id")
@click.option("--admin", is_flag=True, help="Change the /admin bundles, not the room tools.")
@click.option("--add", "add", default=None, help="The tool or bundle to add.")
@click.option("--remove", "remove", default=None, help="The tool or bundle to remove.")
@as_root_option
def groups_tools(surface, chat_id, admin, add, remove):
    """Show or change what the owner granted a room.

    CHAT_ID is negative for Telegram — put `--` before it or use the safe
    `_` alias (see `polyrob owner groups --help`).
    """
    ensure_env_loaded()
    from core.surfaces import group_admin
    if add and remove:
        raise click.UsageError("use --add or --remove, not both")
    if not add and not remove:
        click.echo(group_admin.show_tools(_group_container(write=False), _group_owner_uid(),
                                          surface, chat_id))
        return
    click.echo(group_admin.set_tools(_group_container(write=True), _group_owner_uid(),
                                     surface, chat_id, admin=admin, add=bool(add),
                                     name=add or remove))


@groups.command("audit")
@click.argument("surface")
@click.argument("chat_id")
@click.option("--limit", default=30, show_default=True, help="How many rows.")
@as_root_option
def groups_audit(surface, chat_id, limit):
    """Who ran which admin tool in a room, for the last 7 days.

    CHAT_ID is negative for Telegram — put `--` before it or use the safe
    `_` alias (see `polyrob owner groups --help`).
    """
    ensure_env_loaded()
    from core.surfaces import group_admin
    click.echo(group_admin.audit(_group_container(write=False), surface, chat_id, limit))
