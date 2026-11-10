"""`polyrob owner approve <id>` — decide ANY pending item, as chat `/approve <id>` does.

113 §2 item 5 (I-2): `owner approve` approved a PERSON (a correspondent binding) while
chat `/approve` decides any item of the pending union, and the shell twin of that was
`owner promote KIND ID`. Now `owner approve <id>` (or `all`) decides by the id
`owner pending` shows, through the same resolver the chat verb uses
(``approval_queue.resolve_pending_target``) and the same decider `owner promote` uses
(``approval_queue.decide_pending`` / ``decide_all_pending``). The person form is
`owner person-approve`; the old `approve <surface> <address>` and `approve --all`
still run it, with a note.
"""
import click

from cli._admin_home import as_root_option
from cli.commands._bootstrap import ensure_env_loaded
from cli.commands.owner import (_confirm_signer_review, _data_dir, _decide_all_and_echo,
                                _goal_board, _instance_id, _owner_tenant, _registry, owner)
from cli.commands.owner import approve as _person_approve

PERSON_NOTE = ("note: `owner approve <surface> <address>` and `owner approve --all` are "
               "the person form; its name is now `polyrob owner person-approve`.")


@owner.command("approve")
@click.argument("target", required=False)
@click.argument("address", required=False)
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--all", "approve_all", is_flag=True, hidden=True)
@click.option("--thread", default=None, hidden=True)
@click.option("--all-tenants", "all_tenants", is_flag=True, hidden=True)
@as_root_option
@click.pass_context
def approve(ctx, target, address, user, approve_all, thread, all_tenants):
    """Approve ONE pending item by its id, or `all` — any kind `owner pending` lists.

    TARGET is the id `owner pending` shows (also `kind:id`, or the `p-…` / `tap-…`
    token). The same decision as chat `/approve <id>`; `owner reject` declines.
    To approve a person, use `owner person-approve <surface> <address>`.
    """
    ensure_env_loaded()  # the `owner` group already loaded it; memoized
    if address is not None or approve_all or thread or all_tenants:
        click.echo(click.style(PERSON_NOTE, dim=True), err=True)
        ctx.invoke(_person_approve, surface=target, address=address, approve_all=approve_all,
                   thread=thread, user=user, all_tenants=all_tenants)
        return
    if not target:
        click.echo(click.style("usage: polyrob owner approve <id> | all   "
                               "(the ids are in `polyrob owner pending`)", fg="yellow"))
        raise SystemExit(1)
    tenant = _owner_tenant(user)
    if target.lower() == "all":
        _decide_all_and_echo(True, tenant)
        return
    from tools.controller.approval_queue import (all_pending, decide_pending,
                                                 resolve_pending_target, strip_tap_prefix)
    home = _data_dir(write=True)
    pending = all_pending(user_id=tenant, home_dir=home, instance_id=_instance_id(),
                          board=_goal_board(), correspondent_registry=_registry(home))
    match = resolve_pending_target(target, pending)
    if match is None and strip_tap_prefix(target) is not None:
        # A tool approval that left the open queue: only its lane can say whether it
        # was decided, expired or never existed (the chat verb does the same).
        match = {"kind": "tool_approval", "id": target}
    if match is None:
        msg = f"no pending item '{target}' for tenant {tenant} — see `polyrob owner pending`"
        if pending.unavailable:
            msg += f" ({pending.degraded_line()})"
        click.echo(click.style(msg, fg="yellow"))
        raise SystemExit(1)
    kind, item_id = match.get("kind", ""), match.get("id")
    if kind == "signer_approval" and not _confirm_signer_review(item_id):
        raise SystemExit(1)
    ok, msg = decide_pending(kind, item_id, approve=True, user_id=tenant, home_dir=home,
                             instance_id=_instance_id(), board=_goal_board(),
                             correspondent_registry=_registry(home),
                             rev=match.get("seat_rev"))
    click.echo(click.style(msg, fg="green" if ok else "yellow"))
    if not ok:
        raise SystemExit(1)
