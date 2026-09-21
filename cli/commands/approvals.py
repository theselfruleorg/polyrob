"""`polyrob approvals` — manage the approval-gated action set (owner-UX P2 T4).

The CLI counterpart of the `/approve` REPL command
(``cli/ui/commands/h_approve.py``) — same three verbs (``list``/``add``/
``remove``) over the SAME ``effective_approval_state()`` helper
(``tools/controller/approval.py``) that ``Controller.__init__`` uses to
actually wire the approval hook, so CLI display can never drift from
enforcement.

``add`` writes ``approvals.require`` directly (a union can only ADD a gate —
tightening needs no owner review). ``remove`` can only remove a PREF-added
entry (an env/posture entry is operator-controlled, explained not removed);
since removing loosens policy it queues a guarded ``propose_pref_change``
(Task 3's review queue) instead of writing immediately — apply with
``polyrob owner promote pref_change approvals.require --user <uid>``.

Data-home resolution is the ONE admin seam
(``cli/_admin_home.py::admin_data_dir`` — the 031 rule, which adopts the
DEPLOYED home when the shell declares none), overridable via the hidden
``--home`` option (test/ops only). The tenant is
``core.admin_data_home.admin_owner_principal()``.
"""
from __future__ import annotations

import click

from core.prefs import load_preferences, propose_pref_change, write_preference
from cli._admin_home import admin_data_dir, as_root_option
from tools.controller.approval import effective_approval_state


def _default_home_dir(write: "bool | None" = None) -> str:
    """The DEPLOYED data home (cli/_admin_home.py, the 031 rule) — the approval set is
    the daemon's; resolving the shell-local home here wrote to /root/.polyrob on prod
    and printed success (the same class as the recorded `delivery.daily_cap` defect)."""
    return admin_data_dir(write=write)


def _tenant(user_id: "str | None") -> str:
    """The tenant whose preference row this verb reads or writes.

    C23: every verb here defaulted to the LITERAL ``"local"``. On a box with a
    bound owner the gate is enforced under that owner's tenant, so
    ``polyrob approvals add`` wrote a row the running Controller never read and
    reported a gate that did not exist. ``admin_owner_principal`` is the ONE
    resolver, and it adopts the deployment's declaration.
    """
    if user_id:
        return user_id
    from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
    try:
        return admin_owner_principal()
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))


@click.group("approvals")
def approvals():
    """View/manage the approval-gated action set (list|add|remove)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@approvals.command("list")
@click.option("--user", "user_id", default=None,
              help="Tenant user id (default: this instance's owner)")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def list_cmd(user_id, home_dir_opt, as_json):
    """Show the effective gated-action set: per-entry source + provider."""
    home_dir = home_dir_opt or _default_home_dir(write=False)
    user_id = _tenant(user_id)
    gates, provider = effective_approval_state(user_id, home_dir)
    if as_json:
        import json
        click.echo(json.dumps({"gates": gates, "provider": provider,
                               "user_id": user_id}, indent=2))
        return
    click.echo(click.style(f"approvals — tenant {user_id}", dim=True))
    if not gates:
        from cli.ui.candy import empty
        click.echo(empty("approval gates configured",
                         "every action runs without a tap", yet=False))
    else:
        click.echo(f"{len(gates)} approval gate(s):")
        for action in sorted(gates):
            click.echo(f"  {action}   ({gates[action]})")
    click.echo("")
    click.echo(f"provider: {provider}")
    if provider == "auto" and gates:
        click.echo(
            "⚠ gate(s) registered but approval provider is 'auto' (allow-all) — "
            "set approvals.provider to interactive_cli to enforce"
        )


@approvals.command("add")
@click.argument("action")
@click.option("--user", "user_id", default=None,
              help="Tenant user id (default: this instance's owner)")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
@as_root_option
def add_cmd(action, user_id, home_dir_opt):
    """Add ACTION to approvals.require (safe union — tightens policy only).

    ACTION is not validated against the live tool registry (unreachable from
    the CLI context) — any non-empty name is accepted; double-check spelling.
    """
    home_dir = home_dir_opt or _default_home_dir(write=True)
    user_id = _tenant(user_id)
    action = action.strip()
    if not action:
        raise click.ClickException("ACTION must not be empty")
    current = list(load_preferences(home_dir, user_id).get("approvals.require", []) or [])
    if action in current:
        click.echo(f"'{action}' is already in approvals.require (no change).")
        return
    updated = sorted(set(current) | {action})
    ok, err = write_preference(home_dir, user_id, "approvals.require", updated)
    if not ok:
        raise click.ClickException(err)
    click.echo(
        f"Added '{action}' to approvals.require for tenant {user_id} "
        f"(gate registered). "
        "Note: this action name is not validated against the live tool "
        "registry — that check is skipped from the CLI context; make sure "
        "it's spelled correctly."
    )


@approvals.command("remove")
@click.argument("action")
@click.option("--user", "user_id", default=None,
              help="Tenant user id (default: this instance's owner)")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
@as_root_option
def remove_cmd(action, user_id, home_dir_opt):
    """Queue removal of ACTION from approvals.require for owner review.

    Only a pref-added entry can be removed here — an env/posture entry is
    operator-controlled (edit APPROVAL_REQUIRED_TOOLS / compute posture
    instead). Removing loosens policy, so it's queued rather than written
    immediately: apply with
    ``polyrob owner promote pref_change approvals.require --user <uid>``.
    The proposal is OPERATION-based (``op="remove_entry"``, review fix): it
    stores WHICH entry to drop and the promote recomputes against the CURRENT
    list, so a gate added between propose and promote survives.
    """
    home_dir = home_dir_opt or _default_home_dir(write=True)
    user_id = _tenant(user_id)
    action = action.strip()
    if not action:
        raise click.ClickException("ACTION must not be empty")
    gates, _provider = effective_approval_state(user_id, home_dir)
    source = gates.get(action)
    if source is None:
        click.echo(f"'{action}' is not currently gated (nothing to remove).")
        return
    if source != "pref":
        mechanism = ("required by AGENT_COMPUTE_POSTURE >= 2" if source == "posture"
                     else "set in env APPROVAL_REQUIRED_TOOLS")
        click.echo(
            f"'{action}' is operator-controlled ({mechanism}) — it can't be "
            "removed here."
        )
        return
    ok, result = propose_pref_change(user_id, "approvals.require", None, home_dir,
                                     op="remove_entry", entry=action)
    if not ok:
        raise click.ClickException(result)
    click.echo(
        f"Removing '{action}' from approvals.require loosens policy, so it's "
        f"queued for owner review — apply with `polyrob owner promote "
        f"pref_change {result} --user {user_id}`."
    )
