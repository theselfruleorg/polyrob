"""``polyrob owner invite``: bind a third party to a session, and say what is stored.

The registry's seed is idempotent and keeps an existing row, so the verb reports the
stored binding and never its own argument. ``--rebind`` is the owner's way to move a
contact: the runtime's dead-session resume was the only other caller of that write.
"""
import click

from cli._admin_home import as_root_option
from core.runtime_paths import store_db_path

_ROUTABLE = ("active", "pending")
#: A seed result that bound nothing, with what the owner reads.
_NOT_BOUND = {
    "disabled": "the binding store could not be written",
    "refused": "the per-day cap on new contacts is reached",
    "refused_self": "this address is the agent's own",
}


def _session_id(session_id: str) -> str:
    """The id as typed. The cleaner replaces a short id with a generated one and strips a
    role prefix, and a binding to a value the owner never typed is the fault this verb had."""
    from agents.task.path import pm
    try:
        cleaned = pm().clean_session_id(session_id)
    except ValueError:
        cleaned = ""
    if cleaned != session_id:
        raise click.ClickException(
            f"{session_id!r} is not a session id; check it with `polyrob session list`.")
    return cleaned


def _warn_unknown_session(session_id: str) -> None:
    """Warn and never refuse: the runtime replaces a dead session on the contact's next
    message, and a tree that cannot be read is no proof that the session is missing."""
    from cli.commands.session import _data_root
    from cli.session_paths import session_directory
    try:
        root = _data_root()
        if not root.is_dir():
            return
        found = session_directory(root, session_id)
    except click.ClickException:
        found = None  # several sessions share the prefix, so none has this exact id
    except OSError:
        return
    if found is None or found.name != session_id:
        click.echo(click.style(f"warning: no session {session_id} under {root}; check the id "
                               "with `polyrob session list`", fg="yellow"), err=True)


def _report(surface: str, address: str, row: dict) -> None:
    state = row["state"]
    click.echo(click.style(f"invite {surface}:{address} -> session {row['session_id']}: {state}",
                           fg="green" if state == "active" else "yellow")
               + ("  (run `polyrob owner person-approve` to activate)" if state == "pending" else ""))


def _refuse_expired(surface: str, address: str, row: dict) -> None:
    if row["state"] not in _ROUTABLE:
        raise click.ClickException(
            f"{surface}:{address} was rejected or revoked (session {row['session_id']}); "
            "nothing changed.")


def _rebind(registry, data_dir: str, surface: str, address: str, tenant: str,
            row: dict, session_id: str) -> None:
    """Move every binding of the address and its stored conversation, then record the move
    and tell the owner: an approval names a session, so a move is never silent."""
    import os

    from core.surfaces.conversations import ConversationStore
    from core.surfaces.seed import record_contact_rebound
    old = row["session_id"]
    count = registry.rebind_session(surface=surface, address=address, user_id=tenant,
                                    new_session_id=session_id)
    db = store_db_path("conversations.db", data_dir)
    failure = None
    try:
        if os.path.exists(db):
            ConversationStore(db).rebind_session(tenant, surface, address, session_id)
    except Exception as e:
        failure = f"{type(e).__name__}: {e}"
    record_contact_rebound(tenant, surface, address, old, session_id)
    click.echo(click.style(f"rebound {surface}:{address} from session {old} -> {session_id} "
                           f"({count} binding row(s), state {row['state']})", fg="green"))
    if failure:
        raise click.ClickException(
            f"the binding moved, but the stored conversation did not ({failure}); "
            "run the same command again.")


@click.command("invite")
@click.argument("surface")
@click.argument("address")
@click.argument("session_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--thread", default=None, help="Thread anchor (default: address-keyed)")
@click.option("--rebind", is_flag=True, default=False,
              help="Move every binding of ADDRESS, and its stored conversation, to SESSION_ID.")
@as_root_option
def invite(surface, address, session_id, user, thread, rebind):
    """Register a third party as a correspondent of SESSION_ID (owner-driven seed).

    Their replies then route to that session as DATA. Honours the approval gate +
    per-day cap (a pending invite needs `polyrob owner person-approve`). A contact that is
    already bound stays on its session unless `--rebind` is passed.
    """
    # C6: this used to `os.environ.setdefault("CORRESPONDENT_ACCESS_ENABLED",
    # "true")` — one owner verb silently switching on the capability flag that
    # governs whether a third party may reach the agent AT ALL, for the life of
    # the process, in a way no other seat could see. A verb refuses and names
    # the remedy; it never grants itself the posture it needs.
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from cli.commands.owner import _canon, _data_dir, _owner_tenant, _registry
    from core.surfaces.config import SurfaceConfig
    if not SurfaceConfig.correspondent_access_enabled():
        raise click.ClickException(
            "correspondent access is OFF, so a seeded binding would route "
            "nothing: their reply would be denied at the door.\n"
            "    polyrob config set CORRESPONDENT_ACCESS_ENABLED true --global\n"
            "then re-run this invite (a systemd deploy sets it in its own env "
            "file and needs a restart).")
    if rebind and thread:
        raise click.UsageError("--rebind moves every binding of the address; drop --thread.")
    from core.surfaces.seed import maybe_seed_correspondent

    session_id = _session_id(session_id)
    # ⚠️ The SAME resolver `pending`/`inbox` read (`_owner_tenant`). It seeded
    # under `resolve_owner_principal()` until 2026-09-15, so an owner-seeded
    # invite landed in one tenant and the decision queue read another — a
    # confident zero over a real item, the 035 P0-3 class.
    tenant = _owner_tenant(user)
    data_dir = _data_dir(write=True)
    registry = _registry(data_dir)
    address = _canon(address)   # C22: the key every routing seat reads
    _warn_unknown_session(session_id)
    row = registry.binding(surface=surface, address=address, user_id=tenant, thread_id=thread)
    if rebind and row is None:
        raise click.ClickException(
            f"{surface}:{address} has no binding to move; run this without --rebind.")
    if row is not None:
        _refuse_expired(surface, address, row)
        if row["session_id"] == session_id:
            _report(surface, address, row)
            return
        if rebind:
            _rebind(registry, data_dir, surface, address, tenant, row, session_id)
            return
        raise click.ClickException(
            f"{surface}:{address} is already bound to session {row['session_id']} "
            f"({row['state']}); nothing changed. Move it with:\n"
            f"    polyrob owner invite {surface} {address} {session_id} --rebind")

    class _C:
        def get_service(self, name):
            return registry if name == "correspondent_registry" else None

    state = maybe_seed_correspondent(
        _C(), surface=surface, address=address, session_id=session_id,
        user_id=tenant, thread_id=thread, provenance="owner")
    if state in _NOT_BOUND:
        raise click.ClickException(f"{surface}:{address} is not bound: {_NOT_BOUND[state]}.")
    row = registry.binding(surface=surface, address=address, user_id=tenant, thread_id=thread)
    if row is None:
        raise click.ClickException(f"{surface}:{address} is not bound: no row was stored.")
    _refuse_expired(surface, address, row)
    _report(surface, address, row)
