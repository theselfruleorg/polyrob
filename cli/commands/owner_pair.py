"""`polyrob owner pair` — DM pairing (POLYROB_REQUIRE_PAIRING), attached to `owner`.

``core/pairing.py`` issues one-time codes to unknown senders; these commands are the
operator-side approval path. Same PairingStore + data home the surface dispatcher uses.
"""
import click

from cli._admin_home import as_root_option
from cli.commands._bootstrap import ensure_env_loaded
from cli.commands.owner import _data_dir, owner


def _pairing_store(*, write: "bool | None" = None):
    from core.pairing import store_for
    return store_for(_data_dir(write=write))


@owner.group("pair")
def pair():
    """Approve/inspect DM pairing requests (POLYROB_REQUIRE_PAIRING).

    An approved sender may command the agent on that chat app (the owner tier)
    under their own tenant: never the owner's wallet, approvals, money, memory or
    owner commands.
    Approve only a code that YOUR OWN account received."""
    ensure_env_loaded()


def _pair_row(r) -> str:
    from core.pairing import describe
    if r.get("paired"):
        return describe(r)
    return (f"{describe(r)}"
            f"  (approve: polyrob owner pair approve {r.get('surface') or '<surface>'} {r['code']})")


@pair.command("list")
def pair_list():
    """Every paired user and every waiting request, with its chat app and id."""
    rows = _pairing_store(write=False).list_all()
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("paired users or pending requests", "nobody unknown has written in"))
        return
    for r in rows:
        click.echo(_pair_row(r))


@pair.command("pending")
def pair_pending():
    """List users waiting for pairing approval (with their codes)."""
    rows = [r for r in _pairing_store(write=False).list_all() if not r.get("paired")]
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("pending pairing requests",
                         "nobody unknown has written in"))
        return
    for r in rows:
        click.echo(_pair_row(r))


@pair.command("approve")
@click.argument("args", nargs=-1, required=True)
@as_root_option
def pair_approve(args):
    """Approve a request: `approve <surface> <code>` (the code must come from that
    chat app)."""
    if len(args) == 2:
        surface, code = args[0].strip().lower(), args[1]
    elif len(args) == 1:
        raise click.UsageError("name the chat app the code came from: "
                               "polyrob owner pair approve <surface> <code>")
    else:
        raise click.UsageError("usage: polyrob owner pair approve <surface> <code>")
    store = _pairing_store(write=True)
    row = store.by_code(code) or {}
    uid = store.approve(code, surface=surface)
    if uid is None:
        raise click.ClickException(f"no pending pairing request from {surface} with code {code!r}")
    who = f"{row.get('surface') or '?'}:{row.get('raw_id') or '?'}"
    click.echo(click.style(f"paired {who} (user={uid}): this sender may now command the "
                           f"agent with their own memory, never your wallet or approvals",
                           fg="green"))


@pair.command("revoke")
@click.argument("args", nargs=-1, required=True)
@as_root_option
def pair_revoke(args):
    """Revoke a paired (or pending) user: `revoke <surface> <platform id>`, or
    `revoke <user_id>`."""
    if len(args) == 2:
        n = _pairing_store(write=True).revoke(args[1], surface=args[0].strip().lower())
    elif len(args) == 1:
        n = _pairing_store(write=True).revoke(args[0])
    else:
        raise click.UsageError("usage: polyrob owner pair revoke <surface> <id>")
    if not n:
        raise click.ClickException("no paired or pending user matches")
    click.echo(click.style(f"revoked {' '.join(args)}", fg="green"))
