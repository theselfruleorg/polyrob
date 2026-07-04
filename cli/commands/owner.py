"""polyrob owner — quick access to who can talk to / command the agent.

One place to see the bound owner + per-surface access posture, list the third-party
correspondents the agent is talking to, and approve a pending one. This is the single
admin seam for the WS-A three-tier access model (owner can command; correspondents are
DATA-only; unknown senders are denied).
"""
import os

import click


def _registry(data_dir: str):
    from core.surfaces.correspondents import CorrespondentRegistry
    return CorrespondentRegistry(os.path.join(data_dir, "correspondents.db"))


def _data_dir() -> str:
    """Resolve the SAME data home the surface daemons use (build_cli_container's
    _resolve_cli_data_home = <cwd>/.polyrob by default, or POLYROB_DATA_DIR).

    The old `POLYROB_DATA_DIR or "data"` pointed owner admin at ./data while the
    daemon wrote to <cwd>/.polyrob/correspondents.db — so `owner correspondents/
    approve/invite` silently operated on a DB the surface never read.
    """
    from core.bootstrap import _resolve_cli_data_home
    data_home, _, _ = _resolve_cli_data_home()
    return str(data_home)


@click.group()
def owner():
    """Inspect/manage who can command the agent and who it talks to."""
    # Bootstrap env BEFORE any subcommand reads config: file-set values written by
    # `polyrob config set …` (e.g. POLYROB_OWNER_USER_ID, CORRESPONDENT_ACCESS_ENABLED)
    # live in the .env layer, so without this `owner show` reports "unbound" and
    # `owner invite` can't honour a file-set access posture. Mirrors serve/kb/model
    # (order + local_mode=True).
    from core.bootstrap import load_env, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()
    load_env(local_mode=True)


@owner.command("show")
def show():
    """Show the bound owner and per-surface access posture."""
    from core.surfaces.owner_admin import owner_access_summary
    s = owner_access_summary()
    op = s["owner_principal"] or click.style("(unbound — set POLYROB_OWNER_USER_ID)", fg="yellow")
    click.echo(click.style("owner: ", bold=True) + str(op))
    click.echo(f"correspondent access: {'on' if s['correspondent_access_enabled'] else 'off'}"
               f"  (require approval: {'yes' if s['require_approval'] else 'no'},"
               f" cap/day: {s['max_new_correspondents_per_day']})")
    click.echo(f"owner-by-email: {'on' if s['owner_by_email'] else 'off (v1: forgeable From:)'}")
    surf = ", ".join(f"{k}={'on' if v else 'off'}" for k, v in s["surfaces"].items())
    click.echo(f"surfaces: {surf}")


@owner.command("correspondents")
@click.option("--user", default=None, help="Filter to one tenant user_id")
def correspondents(user):
    """List the third-party correspondents the agent is talking to."""
    rows = _registry(_data_dir()).list(user_id=user)
    if not rows:
        click.echo(click.style("no correspondents", dim=True))
        return
    for r in rows:
        state = r["state"]
        color = {"active": "green", "pending": "yellow", "expired": "red"}.get(state, "white")
        click.echo(f"{click.style(state.ljust(8), fg=color)} "
                   f"{r['surface']}:{r['address']}  -> session {r['session_id']} "
                   f"(tenant {r['user_id']})")


@owner.command("invite")
@click.argument("surface")
@click.argument("address")
@click.argument("session_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--thread", default=None, help="Thread anchor (default: address-keyed)")
def invite(surface, address, session_id, user, thread):
    """Register a third party as a correspondent of SESSION_ID (owner-driven seed).

    Their replies then route to that session as DATA. Honours the approval gate +
    per-day cap (a pending invite needs `polyrob owner approve`).
    """
    import os
    os.environ.setdefault("CORRESPONDENT_ACCESS_ENABLED", "true")
    from core.instance import resolve_owner_principal
    from surfaces.email.seed import maybe_seed_correspondent

    tenant = user or resolve_owner_principal() or "local"

    class _C:
        def get_service(self, name):
            return _registry(_data_dir()) if name == "correspondent_registry" else None

    state = maybe_seed_correspondent(
        _C(), surface=surface, address=address, session_id=session_id,
        user_id=tenant, thread_id=thread, provenance="owner")
    color = {"active": "green", "pending": "yellow"}.get(state, "red")
    click.echo(click.style(f"invite {surface}:{address} -> session {session_id}: {state}",
                           fg=color)
               + ("  (run `polyrob owner approve` to activate)" if state == "pending" else ""))


def _instance_id() -> str:
    from core.instance import resolve_instance_id
    return resolve_instance_id()


def _owner_tenant(user) -> str:
    from core.instance import resolve_owner_principal
    return user or resolve_owner_principal() or "local"


@owner.command("pending")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def pending(user):
    """List the agent's PENDING self-evolution proposals (identity notes + skills).

    These are things the agent learned and quarantined for your review — they change
    nothing until you `owner promote` them.
    """
    from core import self_evolution
    tenant = _owner_tenant(user)
    items = self_evolution.list_pending(tenant, home_dir=_data_dir(),
                                        instance_id=_instance_id())
    if not items:
        click.echo(click.style("no pending proposals", dim=True))
        return
    click.echo(click.style(f"{len(items)} pending proposal(s) for tenant {tenant}:", bold=True))
    for it in items:
        label = "identity" if it["kind"] == "self_context" else "skill"
        click.echo(f"  {click.style(label.ljust(8), fg='yellow')} "
                   f"{click.style(it['kind'] + ':' + str(it['id']), bold=True)}"
                   f"  ({it['chars']} chars)")
        click.echo(f"           {it['preview']}")
    click.echo(click.style("\napprove: ", dim=True)
               + "polyrob owner promote <kind> <id>   "
               + click.style("reject: ", dim=True)
               + "polyrob owner reject <kind> <id>")


@owner.command("promote")
@click.argument("kind")
@click.argument("item_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def promote(kind, item_id, user):
    """Promote a PENDING proposal to active. KIND is 'self_context' or 'skill'."""
    from core import self_evolution
    tenant = _owner_tenant(user)
    ok, msg = self_evolution.promote(kind, item_id, user_id=tenant,
                                     home_dir=_data_dir(), instance_id=_instance_id())
    click.echo(click.style(msg, fg="green" if ok else "yellow"))
    if not ok:
        raise SystemExit(1)


@owner.command("reject")
@click.argument("kind")
@click.argument("item_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def reject(kind, item_id, user):
    """Reject (archive-then-discard) a PENDING proposal. KIND is 'self_context' or 'skill'."""
    from core import self_evolution
    tenant = _owner_tenant(user)
    ok, msg = self_evolution.reject(kind, item_id, user_id=tenant,
                                    home_dir=_data_dir(), instance_id=_instance_id())
    click.echo(click.style(msg, fg="green" if ok else "yellow"))
    if not ok:
        raise SystemExit(1)


@owner.command("approve")
@click.argument("surface")
@click.argument("address")
@click.option("--thread", default=None, help="Thread id (if the correspondent has several)")
@click.option("--user", default=None, help="Scope to one tenant user_id (multi-tenant safety)")
def approve(surface, address, thread, user):
    """Approve a PENDING correspondent so their replies route as DATA."""
    ok = _registry(_data_dir()).approve(surface=surface, address=address,
                                        thread_id=thread, user_id=user)
    if ok:
        click.echo(click.style(f"approved {surface}:{address}", fg="green"))
    else:
        click.echo(click.style(
            f"no pending correspondent {surface}:{address}"
            + (f" (thread {thread})" if thread else ""), fg="yellow"))
