"""polyrob owner — quick access to who can talk to / command the agent.

One place to see the bound owner + per-surface access posture, list the third-party
correspondents the agent is talking to, and approve a pending one. This is the single
admin seam for the WS-A three-tier access model (owner can command; correspondents are
DATA-only; unknown senders are denied).
"""
import json
import logging
import os
import re

import click

from cli._admin_home import as_root_option
from cli.commands._grouped import GroupedGroup

logger = logging.getLogger(__name__)


def _canon(address: str) -> str:
    """The ONE address key (``core.surfaces.address_key.canonical_addr``).

    C22: ``allow``/``deny``/``allowlist`` wrote and read the RAW argument, so
    ``@Handle`` and ``handle`` (and a ``https://t.me/handle`` paste) were three
    different rows, while every routing seat keys on the canonical form — an
    allow that looked granted and denied nothing.
    """
    from core.surfaces.address_key import canonical_addr
    return canonical_addr(address)


def _registry(data_dir: str):
    from core.surfaces.correspondents import CorrespondentRegistry
    return CorrespondentRegistry(os.path.join(data_dir, "correspondents.db"))


def _allowlist(data_dir: str):
    from core.surfaces.outbound_allowlist import OutboundAllowlist
    return OutboundAllowlist(os.path.join(data_dir, "surfaces.db"))


def _do_allow(allowlist, user_id, surface, target, note=""):
    """Pure handler (unit-testable without click): grant SURFACE:TARGET for USER_ID."""
    allowlist.allow(user_id, surface, target, note=note)
    return True


def _do_deny(allowlist, user_id, surface, target):
    """Pure handler: revoke SURFACE:TARGET for USER_ID. True if an active row was revoked."""
    return allowlist.revoke(user_id, surface, target)


def _do_allowlist(allowlist, user_id):
    """Pure handler: list allowlist rows for USER_ID."""
    return allowlist.list(user_id)


def _data_dir(write: "bool | None" = None) -> str:
    """Resolve the SAME data home the surface daemons use — via the ONE core
    seam ``core.admin_data_home.admin_data_home``. The old `POLYROB_DATA_DIR or
    "data"` pointed owner admin at ./data while the daemon wrote to
    <cwd>/.polyrob/correspondents.db — so `owner correspondents/approve/invite`
    silently operated on a DB the surface never read. Never re-implement
    resolution here.

    031: the seam is `admin_data_home`, not `resolve_data_home` — the latter
    never applies the server default, so an owner SSHed into the production box
    with no `POLYROB_DATA_DIR` in their shell halted `~/.polyrob` and was told it
    worked. `admin_data_home` adopts the deployed home when it can read it and
    REFUSES when it cannot; a purely local box is unchanged and silent.

    057 WS-G / C40: *write* declares the caller's INTENT so the euid guard can
    apply. A mutating verb passes ``write=True`` (root on a deployed box is
    refused with the ``sudo -u polyrob-agent`` remedy); a read passes
    ``write=False`` (never refused). ``None`` means the verb has not been
    classified and root gets one warning — honest, but no verb should stay
    there.
    """
    from cli._admin_home import admin_data_dir
    return admin_data_dir(write=write)


# D7 (proposal 030): sectioned --help instead of one flat alphabetical wall.
_OWNER_HELP_SECTIONS = [
    ("Access & pairing",
     ["correspondents", "invite", "approve", "allow", "deny", "allowlist",
      "pair", "groups"]),
    ("Pending & asks",
     # 043 D1: `inbox` LEADS this section — it is the union the other five are
     # pieces of, and a person looking for "what needs me" should meet it first.
     ["inbox", "pending", "show-pending", "promote", "reject", "asks",
      "fulfill", "missed"]),
    # 046: `paid` is MONEY — it prices what a room sells and names what the
    # treasury OWES a payer whose effect never landed.
    ("Money", ["invoices", "settle", "sub", "paid"]),
    ("Control", ["halt", "resume", "pause-entries", "resume-entries",
                 "pause-streams", "resume-streams", "show"]),
]


@click.group(cls=GroupedGroup, help_sections=_OWNER_HELP_SECTIONS)
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
    # C33: the summary now adopts the DEPLOYED binding when this shell declares
    # none, and SAYS which it used — "(unbound)" printed in an SSH shell over a
    # box that has answered to an owner for months was the same confident-wrong
    # answer the 031 data home and the 035 instance/tenant axes each produced.
    if s["owner_principal"]:
        op = str(s["owner_principal"])
        if s.get("owner_source") == "deployed":
            op += click.style("  (from the deployed env file — not set in this shell)",
                              dim=True)
    else:
        op = click.style("(unbound — run `polyrob init`, or set the owner on the "
                         "deployment and re-run)", fg="yellow")
    click.echo(click.style("owner: ", bold=True) + op)
    from agents.task.constants import autonomy_mode_display
    click.echo(f"autonomy mode: {autonomy_mode_display()}")
    click.echo(f"correspondent access: {'on' if s['correspondent_access_enabled'] else 'off'}"
               f"  (require approval: {'yes' if s['require_approval'] else 'no'},"
               f" cap/day: {s['max_new_correspondents_per_day']})")
    click.echo(f"owner-by-email: {'on' if s['owner_by_email'] else 'off (v1: forgeable From:)'}")
    surf = ", ".join(f"{k}={'on' if v else 'off'}" for k, v in s["surfaces"].items())
    click.echo(f"surfaces: {surf}")


@owner.command("halt")
@as_root_option
def halt_cmd():
    """Pause EVERYTHING autonomous now (alias of `polyrob autonomy pause`).

    Writes the ONE 031 pause record (<data>/AUTONOMY_PAUSE.json); every autonomous
    loop, trade, and payment refuses while it holds. Lift it with
    `polyrob owner resume` / `polyrob autonomy resume`.
    """
    from core.surfaces.owner_admin import pause_autonomy, render_pause_result
    res = pause_autonomy(_data_dir(write=True), scopes=("all",),
                         reason="`polyrob owner halt`", via="cli")
    click.echo(render_pause_result(res, resume_hint="`polyrob owner resume`",
                                   status_hint="`polyrob autonomy status`", chat=False))


@owner.command("resume")
@as_root_option
def resume_cmd():
    """Lift every pause (alias of `polyrob autonomy resume`)."""
    from core.surfaces.owner_admin import render_resume_result, resume_autonomy_scopes
    res = resume_autonomy_scopes(_data_dir(write=True), via="cli")
    click.echo(render_resume_result(res, halt_hint="`polyrob owner halt`"))


@owner.command("pause-entries")
@as_root_option
def pause_entries_cmd():
    """Refuse NEW treasury positions while still allowing exits (no restart needed).

    The `trading` scope of the 031 pause record: every treasury entry — ad-hoc
    goal, stream-manifest goal, cron, owner-direct — refuses while it holds.
    Sells to the chain's quote asset and revokes still run. Lift it with
    `polyrob owner resume-entries`.
    """
    from core.surfaces.owner_admin import pause_autonomy, render_pause_result
    res = pause_autonomy(_data_dir(write=True), scopes=("trading",),
                         reason="`polyrob owner pause-entries`", via="cli")
    click.echo(render_pause_result(res, resume_hint="`polyrob owner resume-entries`",
                                   status_hint="`polyrob autonomy status`", chat=False))


@owner.command("resume-entries")
@as_root_option
def resume_entries_cmd():
    """Lift the entry-pause set by `polyrob owner pause-entries`."""
    from core.surfaces.owner_admin import render_resume_result, resume_autonomy_scopes
    res = resume_autonomy_scopes(_data_dir(write=True), scopes=("trading",), via="cli")
    click.echo(render_resume_result(res, halt_hint="`polyrob owner pause-entries`"))


@owner.command("pause-streams")
@as_root_option
def pause_streams_cmd():
    """Refuse NEW stream-manifest reseeds (no restart needed).

    The `streams` scope of the 031 pause record: every declared stream
    (`data/streams/streams.yaml`) refuses to seed fresh goals on its cadence while
    it holds. Goals already live, ad-hoc goals, and cron are untouched. Lift it
    with `polyrob owner resume-streams`.
    """
    from core.surfaces.owner_admin import pause_autonomy, render_pause_result
    res = pause_autonomy(_data_dir(write=True), scopes=("streams",),
                         reason="`polyrob owner pause-streams`", via="cli")
    click.echo(render_pause_result(res, resume_hint="`polyrob owner resume-streams`",
                                   status_hint="`polyrob autonomy status`", chat=False))


@owner.command("resume-streams")
@as_root_option
def resume_streams_cmd():
    """Lift the stream-seeding pause set by `polyrob owner pause-streams`."""
    from core.surfaces.owner_admin import render_resume_result, resume_autonomy_scopes
    res = resume_autonomy_scopes(_data_dir(write=True), scopes=("streams",), via="cli")
    click.echo(render_resume_result(res, halt_hint="`polyrob owner pause-streams`"))


@owner.command("correspondents")
@click.option("--user", default=None, help="Filter to one tenant user_id "
                                           "(default: this instance's owner)")
@click.option("--all-tenants", "all_tenants", is_flag=True, default=False,
              help="EVERY tenant's correspondents (multi-tenant admin).")
@click.option("--history", "history", nargs=2, default=None,
              metavar="SURFACE ADDRESS",
              help="Show the stored transcript with ONE correspondent instead "
                   "of the listing (E10 — the `contact_history` action's seat).")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def correspondents(user, all_tenants, history, as_json):
    """List the third-party correspondents the agent is talking to.

    Scoped to this instance's owner tenant; ask for `--all-tenants` to see
    every bucket on the box.
    """
    # C3: the default used to be user_id=None = EVERY tenant, which on a
    # multi-tenant deploy is another tenant's contact list.
    tenant = None if all_tenants else _owner_tenant(user)
    if history:
        _echo_contact_history(tenant or _owner_tenant(user), history[0],
                              history[1], as_json=as_json)
        return
    rows = _registry(_data_dir(write=False)).list(user_id=tenant)
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    scope = "ALL tenants" if all_tenants else f"tenant {tenant}"
    click.echo(click.style(f"correspondents — scope: {scope}", dim=True))
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("correspondents", "the agent has not written to anyone "
                                           "from this tenant"))
        return
    for r in rows:
        state = r["state"]
        color = {"active": "green", "pending": "yellow", "expired": "red"}.get(state, "white")
        click.echo(f"{click.style(state.ljust(8), fg=color)} "
                   f"{r['surface']}:{r['address']}  -> session {r['session_id']} "
                   f"(tenant {r['user_id']})")


def _echo_contact_history(tenant: str, surface: str, address: str, *,
                          as_json: bool) -> None:
    """E10: the stored transcript with one correspondent.

    The SAME ``ConversationStore`` the agent's read-only ``contact_history``
    action renders — that action had no owner seat at all, so the only way to
    read what the agent had said to a third party was to open the sqlite file.
    An unreadable store is NAMED; it is never rendered as an empty conversation.
    """
    import os as _os

    from core.surfaces.conversations import ConversationStore
    db = _os.path.join(_data_dir(write=False), "conversations.db")
    addr = _canon(address)
    if not _os.path.exists(db):
        msg = (f"no conversation store yet ({db}) — nothing has been recorded "
               f"for any correspondent. That is 'no record', not 'no contact'.")
        click.echo(json.dumps({"error": msg}, indent=2) if as_json
                   else click.style(msg, fg="yellow"))
        return
    try:
        store = ConversationStore(db)
        rows = store.history(tenant, surface, addr)
    except Exception as exc:
        raise click.ClickException(
            f"the conversation store could not be read ({type(exc).__name__}: "
            f"{exc}). That is UNKNOWN, not an empty conversation.")
    if as_json:
        click.echo(json.dumps({"tenant": tenant, "surface": surface,
                               "address": addr, "messages": rows},
                              indent=2, default=str))
        return
    click.echo(click.style(f"{surface}:{addr} — tenant {tenant}", bold=True))
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("messages on record",
                         "the binding exists; nothing has been said through it"))
        return
    from core.surfaces.conversations import _iso
    for m in rows:
        who = m.get("direction") or "?"
        body = str(m.get("body") or "")
        click.echo(f"  [{_iso(m.get('ts') or 0)}] {who:<8} {body}")


@owner.command("invite")
@click.argument("surface")
@click.argument("address")
@click.argument("session_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--thread", default=None, help="Thread anchor (default: address-keyed)")
@as_root_option
def invite(surface, address, session_id, user, thread):
    """Register a third party as a correspondent of SESSION_ID (owner-driven seed).

    Their replies then route to that session as DATA. Honours the approval gate +
    per-day cap (a pending invite needs `polyrob owner approve`).
    """
    # C6: this used to `os.environ.setdefault("CORRESPONDENT_ACCESS_ENABLED",
    # "true")` — one owner verb silently switching on the capability flag that
    # governs whether a third party may reach the agent AT ALL, for the life of
    # the process, in a way no other seat could see. A verb refuses and names
    # the remedy; it never grants itself the posture it needs.
    from core.surfaces.config import SurfaceConfig
    if not SurfaceConfig.correspondent_access_enabled():
        raise click.ClickException(
            "correspondent access is OFF, so a seeded binding would route "
            "nothing: their reply would be denied at the door.\n"
            "    polyrob config set CORRESPONDENT_ACCESS_ENABLED true --global\n"
            "then re-run this invite (a systemd deploy sets it in its own env "
            "file and needs a restart).")
    from core.surfaces.seed import maybe_seed_correspondent

    # ⚠️ The SAME resolver `pending`/`inbox` read (`_owner_tenant`). It seeded
    # under `resolve_owner_principal()` until 2026-09-15, so an owner-seeded
    # invite landed in one tenant and the decision queue read another — a
    # confident zero over a real item, the 035 P0-3 class.
    tenant = _owner_tenant(user)

    class _C:
        def get_service(self, name):
            return (_registry(_data_dir(write=True))
                    if name == "correspondent_registry" else None)

    address = _canon(address)   # C22: the key every routing seat reads
    state = maybe_seed_correspondent(
        _C(), surface=surface, address=address, session_id=session_id,
        user_id=tenant, thread_id=thread, provenance="owner")
    color = {"active": "green", "pending": "yellow"}.get(state, "red")
    click.echo(click.style(f"invite {surface}:{address} -> session {session_id}: {state}",
                           fg=color)
               + ("  (run `polyrob owner approve` to activate)" if state == "pending" else ""))


def _instance_id() -> str:
    """035 P0-3: adopt the DEPLOYED instance id, exactly as ``_data_dir()`` adopts
    the deployed data home.

    Before this, `polyrob owner pending` on the production box resolved the right
    home under instance "polyrob" (the default) while the service runs instance
    "rob", and printed a confident "no pending proposals" over four real ones —
    under a note assuring the owner it had used the deployed home. Never
    re-implement resolution here; the seam is `core.admin_data_home`.
    """
    from core.admin_data_home import AmbiguousDataHome, admin_instance_id
    try:
        return admin_instance_id()
    except AmbiguousDataHome as exc:   # an unreadable env file is a refusal, not a guess
        raise click.ClickException(str(exc))


def _owner_tenant(user) -> str:
    """The owner tenant, adopting the deployment's when the shell is silent (035
    P0-3 — the second axis of the same defect as `_instance_id`).

    `admin_owner_principal` is typed `-> str` and reads the ONE resolver
    (`core.instance.resolve_owner_user_id`) when nothing is declared, so there is
    no `or` fallback left to write here."""
    from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
    if user:
        return user
    try:
        return admin_owner_principal()
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))


def _allowlist_tenant(user) -> str:
    """Tenant resolution for the allow/deny/allowlist commands ONLY.

    These commands must write under the SAME tenant the `message` action reads at
    runtime — a local REPL session's user_id is `core.identity.resolve_identity()`,
    which since 2026-09-15 delegates to the ONE owner-tenant resolver
    (`core.instance.resolve_owner_user_id`: bound owner -> `POLYROB_LOCAL_OWNER`
    -> `local`).

    ⚠️ This is NOT identical to `_owner_tenant`, which additionally adopts the
    tenant a DEPLOYED env file declares — the two differ only in an owner's SSH
    shell on a box whose service env it cannot see. The old reason for the split
    (`_owner_tenant` defaulting to the instance id) is gone.
    """
    from core.identity import resolve_identity
    return user or resolve_identity()


def _money_tenant(user) -> str:
    """M16 (2026-07-15): the ONE tenant resolver for the money listings
    (`owner sub`, and shared with `polyrob finance`).

    The agent's money rows (x402 invoices, subscriptions) are created under the
    runtime session's user_id = ``core.identity.resolve_identity()`` (owner-if-
    bound else "local") — the SAME resolver `polyrob finance` uses. `_owner_tenant`
    resolved to the instance id when unbound, which read a DIFFERENT bucket, so the
    sibling money views disagreed on an unbound install. Both land on the ONE
    resolver since 2026-09-15; this stays the money listings' seam so the scope is
    printed and finance and `owner sub` cannot drift apart again.
    """
    from core.identity import resolve_identity
    return user or resolve_identity()


#: Shared with the Telegram `/pending` verb — one listing, two seats
#: (`core.surfaces.owner_admin.pending_correspondent_items`). Kept as a
#: module-level name because existing callers/tests import it from here.
from core.surfaces.owner_admin import (  # noqa: E402
    pending_correspondent_items as _pending_correspondent_items,
)


@owner.command("inbox")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("-n", "limit", type=int, default=None,
              help="Show only the first N of each section.")
def inbox(user, limit):
    """Everything waiting on a decision from you, blocking first (043 D1).

    The union `owner pending` never was: self-evolution proposals, queued tool
    and spend approvals, pending correspondents, OPEN asks and pending apps —
    composed once (`core.surfaces.inbox`) and rendered by the same function the
    REPL's `/inbox`, Telegram's `/inbox` and the console's Inbox page use.

    The number is DECISIONS; something listed as "not blocking" is there
    because you may want to act, not because the agent is stuck. A store that
    refuses to open is NAMED and the count becomes a floor — this never prints
    "nothing needs you" over a list it could not read.
    """
    from core.surfaces.inbox_render import CLI_REMEDIES, render_inbox
    from surfaces.inbox_sources import build_inbox
    tenant = _owner_tenant(user)
    try:
        body = build_inbox(tenant, data_dir=_data_dir(write=False),
                           instance_id=_instance_id())
    except Exception as exc:
        raise click.ClickException(
            f"the inbox could not be composed ({exc}). That is UNKNOWN, not "
            f"'nothing needs you'.")
    # C21: this rendered REPL_REMEDIES, so every card told the operator to run
    # `/pending approve …` — a slash verb that is not a command at a shell
    # prompt. The CLI's table names `polyrob owner …` verbs.
    click.echo(render_inbox(body, remedies=CLI_REMEDIES, limit=limit))


@owner.command("pending")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def pending(user, as_json):
    """List the agent's PENDING self-evolution proposals (identity notes + skills),
    queued tool-approval requests (Task 9 / G-2 — PAYMENT_APPROVAL_MODE=approve),
    AND pending correspondent bindings (their replies are unroutable until approved).

    These are things the agent learned/asked and quarantined for your review — they
    change nothing until you `owner promote` (or reject) them.
    """
    from core import self_evolution
    from tools.controller.approval_queue import all_pending
    tenant = _owner_tenant(user)
    # 2026-09-15: the ONE union, so this seat and the chat seat list AND decide
    # over the same set, and an unreadable store is NAMED rather than silently
    # dropped from the count.
    pending_set = all_pending(user_id=tenant, home_dir=_data_dir(write=False),
                              instance_id=_instance_id(), board=_goal_board(),
                              correspondent_registry=_registry(_data_dir(write=False)))
    items = pending_set.items
    if as_json:
        # The JSON shape stays the bare list a consumer already parses. An
        # unreadable source is reported on STDERR instead — honest, and it
        # cannot break a script that pipes stdout into a parser.
        if pending_set.unavailable:
            click.echo(pending_set.degraded_line(), err=True)
        click.echo(json.dumps(items, indent=2, default=str))
        return
    if not items:
        from cli.ui.candy import empty
        click.echo(click.style(pending_set.degraded_line(), fg="yellow")
                   if pending_set.unavailable
                   else empty("pending proposals",
                              "nothing is quarantined for your review"))
        return
    click.echo(click.style(f"{len(items)} pending proposal(s) for tenant {tenant}:", bold=True))
    for it in items:
        # The shared self-evolution label map (owner-UX P2-4 final review, item 4)
        # covers the five self-evolution kinds; "correspondent"/"tool_approval"
        # ride separate, non-self-evolution pipelines aggregated into this same
        # list, so they keep their own labels rather than falling back to "skill".
        label = {"correspondent": "contact",
                 "tool_approval": "approval"}.get(
            it["kind"], self_evolution.pending_kind_label(it["kind"]))
        click.echo(f"  {click.style(label.ljust(8), fg='yellow')} "
                   f"{click.style(it['kind'] + ':' + str(it['id']), bold=True)}"
                   f"  ({it['chars']} chars)")
        click.echo(f"           {it['preview']}")
        # 035 P0-5: a pending rule that contradicts an ACTIVE one must be loud —
        # the 09-08 den directives were silently out-ranked by a stale active doc.
        for c in (it.get("conflicts") or []):
            click.echo("           " + click.style(f"⚠ CONFLICT — {c}", fg="red"))
    if pending_set.unavailable:
        click.echo(click.style(pending_set.degraded_line(), fg="yellow"))
    click.echo(click.style("\napprove: ", dim=True)
               + "polyrob owner promote <kind> <id>   "
               + click.style("reject: ", dim=True)
               + "polyrob owner reject <kind> <id>   "
               + click.style("all: ", dim=True)
               + "polyrob owner promote all")


@owner.command("show-pending")
@click.argument("kind")
@click.argument("item_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def show_pending(kind, item_id, user):
    """Show the FULL body of one pending proposal before deciding (T3-09).

    KIND is 'self_context' or 'skill'. `owner pending` shows only a ~160-char
    preview — review the whole quarantined body here, then promote/reject.
    """
    from core import self_evolution
    tenant = _owner_tenant(user)
    ok, body = self_evolution.show(kind, item_id, user_id=tenant,
                                   home_dir=_data_dir(write=False),
                                   instance_id=_instance_id())
    if not ok:
        click.echo(click.style(body, fg="yellow"))
        raise SystemExit(1)
    click.echo(click.style(f"--- pending {kind}:{item_id} ---", bold=True))
    click.echo(body)


def _decide_all_and_echo(approve: bool, tenant: str) -> None:
    """035 P1-10 — `owner promote all` / `owner reject all`.

    Friction when the queue has grown is exactly the state that produced the
    09-08 incident (four proposals, none reviewed). This matches the existing
    `owner approve --all` behavior for correspondents.
    """
    from tools.controller.approval_queue import decide_all_pending
    # 2026-09-15: "all" used to mean the self-evolution THIRD of the queue while
    # the listing right above it showed all three, so a queued payment approval
    # or a pending contact survived an "approve all" with no trace. ONE decider.
    ok_n, fail_n, msgs = decide_all_pending(
        approve=approve, user_id=tenant, home_dir=_data_dir(write=True),
        instance_id=_instance_id(), board=_goal_board(),
        correspondent_registry=_registry(_data_dir(write=True)))
    if not msgs:
        click.echo(click.style("no pending proposals", dim=True))
        return
    for m in msgs:
        click.echo("  " + click.style(m, fg="green" if m.startswith("✓") else "yellow"))
    verb = "promoted" if approve else "rejected"
    click.echo(click.style(f"{ok_n} {verb}, {fail_n} failed", bold=True))
    if fail_n:
        raise SystemExit(1)


@owner.command("promote")
@click.argument("kind")
@click.argument("item_id", required=False)
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@as_root_option
def promote(kind, item_id, user):
    """Promote a PENDING proposal to active, or APPROVE a queued tool-approval
    request. KIND is 'self_context', 'skill', or 'tool_approval' (Task 9 / G-2 —
    ITEM_ID is the tap-<id> shown by `owner pending`). KIND 'all' promotes every
    pending item at once — proposals, queued approvals AND contacts (035 P1-10;
    ITEM_ID is then unused)."""
    tenant = _owner_tenant(user)
    if kind == "all":
        _decide_all_and_echo(True, tenant)
        return
    # ONE decider for every kind in the queue (2026-09-15) — this seat used to
    # special-case `tool_approval` and hand every other kind, `correspondent`
    # included, to the self-evolution promoter, which answered "unknown kind".
    from tools.controller.approval_queue import decide_pending
    ok, msg = decide_pending(kind, item_id, approve=True, user_id=tenant,
                             home_dir=_data_dir(write=True),
                             instance_id=_instance_id(), board=_goal_board(),
                             correspondent_registry=_registry(_data_dir(write=True)))
    click.echo(click.style(msg, fg="green" if ok else "yellow"))
    if not ok:
        raise SystemExit(1)


@owner.command("reject")
@click.argument("kind")
@click.argument("item_id", required=False)
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@as_root_option
def reject(kind, item_id, user):
    """Reject (archive-then-discard) a PENDING proposal, or DECLINE a queued
    tool-approval request. KIND is 'self_context', 'skill', or 'tool_approval'
    (Task 9 / G-2 — ITEM_ID is the tap-<id> shown by `owner pending`). KIND 'all'
    rejects every pending item at once — proposals, queued approvals AND contacts
    (035 P1-10)."""
    tenant = _owner_tenant(user)
    if kind == "all":
        _decide_all_and_echo(False, tenant)
        return
    # ONE decider for every kind in the queue (2026-09-15) — this seat used to
    # special-case `tool_approval` and hand every other kind, `correspondent`
    # included, to the self-evolution promoter, which answered "unknown kind".
    from tools.controller.approval_queue import decide_pending
    ok, msg = decide_pending(kind, item_id, approve=False, user_id=tenant,
                             home_dir=_data_dir(write=True),
                             instance_id=_instance_id(), board=_goal_board(),
                             correspondent_registry=_registry(_data_dir(write=True)))
    click.echo(click.style(msg, fg="green" if ok else "yellow"))
    if not ok:
        raise SystemExit(1)


def _goal_board():
    """The board under the SAME home ``_data_dir()`` adopts — the asks and the
    correspondents of one `pending` listing must come from one data home."""
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(_data_dir(write=None)))


@owner.command("asks")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def asks(user, as_json):
    """List the agent's OPEN asks — concrete needs blocking its progress (§7.2b).

    Fulfil one with `polyrob owner fulfill <id>` after providing what it asks for;
    that flips its blocked goals back to ready so work resumes.

    Tool-approval requests (Task 9 / G-2) have their OWN surface — see
    `polyrob owner pending` / `owner promote tool_approval <id>` — and are
    excluded here so one isn't shown twice under two different id shapes.
    """
    from agents.task.goals.board import ASK_OPEN
    tenant = _owner_tenant(user)
    rows = [a for a in _goal_board().asks(user_id=tenant, status=ASK_OPEN)
            if (a.payload or {}).get("ask_kind") != "tool_approval"]
    if as_json:
        from dataclasses import asdict
        click.echo(json.dumps([asdict(a) for a in rows], indent=2, default=str))
        return
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("open asks", "nothing is blocking the agent"))
        return
    click.echo(click.style(f"{len(rows)} open ask(s) for tenant {tenant}:", bold=True))
    for a in rows:
        blocks = (a.payload or {}).get("blocks_goal_ids", [])
        click.echo(f"  {click.style(a.id, fg='cyan')}  {click.style(a.title, bold=True)}"
                   + (f"  (blocks {len(blocks)} goal(s))" if blocks else ""))
        if a.body:
            click.echo(f"           {a.body[:200]}")
    click.echo(click.style("\nfulfill: ", dim=True) + "polyrob owner fulfill <id>")


@owner.command("fulfill")
@click.argument("ask_id")
@click.argument("answer", nargs=-1)
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@as_root_option
def fulfill(ask_id, answer, user):
    """Mark an ask FULFILLED and flip its blocked goals back to ready.

    Words after the id are your ANSWER (A27): they are kept on the ask and
    handed to the unblocked goal's retry prompt, so the run learns what you
    said, not only that it may go on.
    """
    tenant = _owner_tenant(user)
    ok, unblocked = _goal_board().decide_ask(
        ask_id, user_id=tenant, approved=True, answer=" ".join(answer).strip())
    if not ok:
        click.echo(click.style(f"no open ask '{ask_id}' for tenant {tenant}", fg="yellow"))
        raise SystemExit(1)
    click.echo(click.style(
        f"ask {ask_id} fulfilled — {unblocked} goal(s) unblocked", fg="green"))


@owner.command("missed")
@click.option("-n", "count", default=5, type=int,
              help="How many notices to show (1-20, default 5).")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def missed(count, user):
    """Show owner notices the delivery rail could not send live (A7 / A40).

    The rail never silently drops a message it could not deliver — it records
    a durable ``owner_notice`` instead: suppressed by the daily cap, held by
    an active owner pause, or undelivered (no live sink / send failed). The
    SAME rows Telegram `/missed` and the REPL `/missed` read.
    """
    from core.surfaces.missed import format_notice_lines, missed_notices
    n = max(1, min(20, int(count)))
    tenant = _owner_tenant(user)
    try:
        rows = missed_notices(tenant, _data_dir(), n)
    except Exception as e:
        raise click.ClickException(f"missed notices unavailable ({type(e).__name__}: {e})")
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("missed owner messages on record",
                         "every notice the rail raised was delivered"))
        return
    click.echo(click.style(
        f"Last {len(rows)} missed owner message(s) (newest first):", bold=True))
    for r in rows:
        kind = r.get("kind") or "capped"
        text = str(r.get("text") or "")
        # Wrapped, never clipped — /missed exists to recover text the owner
        # never received live (fix round 1, 2026-09-14).
        for line in format_notice_lines(r.get("ts") or 0, kind, text, gutter="  "):
            click.echo(line)
    click.echo(click.style("\nRaise the cap: ", dim=True)
               + "polyrob config set delivery.daily_cap N --global")


def _do_approve_all(registry, user_id=None, surface=None):
    """Pure handler (unit-testable without click): approve every PENDING binding
    (optionally scoped to one tenant and/or one surface). Returns the count.
    Approvals go row-by-row with each row's OWN tenant, so the cross-tenant
    promotion guard in registry.approve is never bypassed."""
    approved = 0
    try:
        for r in registry.list(user_id=user_id):
            if r.get("state") != "pending":
                continue
            if surface and r.get("surface") != surface:
                continue
            if registry.approve(surface=r["surface"], address=r["address"],
                                thread_id=r.get("thread_id") or None,
                                user_id=r["user_id"]):
                approved += 1
    except Exception:
        # L10 (2026-07-15): was a silent `except: pass` — a mid-loop registry
        # error would silently under-approve with no signal. Log it; the count
        # returned reflects what actually succeeded.
        logger.warning("owner approve --all: bulk approve failed mid-loop", exc_info=True)
    return approved


@owner.command("approve")
@click.argument("surface", required=False)
@click.argument("address", required=False)
@click.option("--all", "approve_all", is_flag=True, default=False,
              help="Approve ALL pending correspondents (optionally filtered by "
                   "SURFACE argument / --user)")
@click.option("--thread", default=None, help="Thread id (if the correspondent has several)")
@click.option("--user", default=None, help="Scope to one tenant user_id "
                                           "(default: this instance's owner)")
@click.option("--all-tenants", "all_tenants", is_flag=True, default=False,
              help="With --all: approve across EVERY tenant on this box.")
@as_root_option
def approve(surface, address, approve_all, thread, user, all_tenants):
    """Approve a PENDING correspondent so their replies route as DATA.

    Single: polyrob owner approve <surface> <address>
    Bulk:   polyrob owner approve --all [<surface>] [--user tenant]

    Bulk approval is scoped to this instance's owner tenant unless you ask for
    `--all-tenants`.
    """
    # C3: `--all` defaulted to user_id=None = every tenant on the box, so one
    # owner's bulk approve activated another tenant's pending contacts.
    tenant = None if all_tenants else _owner_tenant(user)
    if approve_all:
        n = _do_approve_all(_registry(_data_dir(write=True)), user_id=tenant,
                            surface=surface)
        scope = "ALL tenants" if all_tenants else f"tenant {tenant}"
        color = "green" if n else "yellow"
        click.echo(click.style(
            f"approved {n} pending correspondent(s) — scope: {scope}", fg=color))
        return
    if not surface or not address:
        click.echo(click.style(
            "usage: polyrob owner approve <surface> <address>  (or --all)", fg="yellow"))
        raise SystemExit(1)
    address = _canon(address)   # C22: the key the registry and routing agree on
    reg = _registry(_data_dir(write=True))
    ok = reg.approve(surface=surface, address=address,
                     thread_id=thread, user_id=tenant)
    if ok:
        click.echo(click.style(f"approved {surface}:{address}", fg="green"))
        return
    # C34: a failed approve printed "no pending correspondent …" for BOTH
    # absence and ambiguity, so an address with several threads read as a
    # contact that does not exist. Say which it is.
    try:
        rows = [r for r in reg.list(user_id=tenant)
                if r.get("surface") == surface and r.get("address") == address]
    except Exception as exc:
        raise click.ClickException(
            f"the correspondent registry could not be re-read ({exc}); whether "
            f"{surface}:{address} exists is UNKNOWN, not 'no such contact'.")
    pending_rows = [r for r in rows if r.get("state") == "pending"]
    if not rows:
        click.echo(click.style(
            f"no correspondent {surface}:{address} for tenant {tenant}"
            + (f" (thread {thread})" if thread else ""), fg="yellow"))
    elif not pending_rows:
        states = ", ".join(sorted({str(r.get("state")) for r in rows}))
        click.echo(click.style(
            f"{surface}:{address} exists for tenant {tenant} but is {states}, "
            f"not pending — there is nothing to approve.", fg="yellow"))
    else:
        threads = ", ".join(sorted({str(r.get("thread_id") or "(none)")
                                    for r in pending_rows}))
        click.echo(click.style(
            f"{surface}:{address} has {len(pending_rows)} pending binding(s) and "
            f"the request was AMBIGUOUS — pass --thread <id>. Threads: {threads}",
            fg="yellow"))
    raise SystemExit(1)


@owner.command("allow")
@click.argument("surface")
@click.argument("target")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--note", default="", help="Optional note (e.g. why this target is allowed)")
@as_root_option
def allow(surface, target, user, note):
    """Allow the agent to send outbound messages to SURFACE:TARGET."""
    tenant = _allowlist_tenant(user)
    # C22: the raw argument was stored, so `@handle`, `handle` and a t.me paste
    # were three rows while the send gate reads the canonical key — an allow
    # that reported success and permitted nothing.
    target = _canon(target)
    _do_allow(_allowlist(_data_dir(write=True)), tenant, surface, target, note=note)
    click.echo(click.style(f"allowed {surface}:{target} for tenant {tenant}", fg="green"))


@owner.command("deny")
@click.argument("surface")
@click.argument("target")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@as_root_option
def deny(surface, target, user):
    """Revoke outbound permission for SURFACE:TARGET."""
    tenant = _allowlist_tenant(user)
    target = _canon(target)   # C22: same key on write and on revoke
    ok = _do_deny(_allowlist(_data_dir(write=True)), tenant, surface, target)
    if ok:
        click.echo(click.style(f"denied {surface}:{target} for tenant {tenant}", fg="green"))
    else:
        click.echo(click.style(
            f"no active allowlist entry {surface}:{target} for tenant {tenant}", fg="yellow"))


@owner.command("allowlist")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def allowlist(user, as_json):
    """List the outbound-send allowlist for a tenant."""
    tenant = _allowlist_tenant(user)
    rows = _do_allowlist(_allowlist(_data_dir(write=False)), tenant)
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("allowlist entries",
                         f"tenant {tenant} may not be written to yet"))
        return
    for r in rows:
        color = "green" if r["status"] == "active" else "red"
        note = f"  ({r['note']})" if r["note"] else ""
        click.echo(f"{click.style(r['status'].ljust(8), fg=color)} "
                   f"{r['surface']}:{r['target']}{note}")


# --- Money loop: invoice admin ----------------------------------------------

def _bot_db_path():
    """The live ``bot.db`` (the x402 invoice home): ``DB_PATH`` wins, else the
    first existing candidate layout under the ADMIN data home.

    C11: this resolved through ``core.bootstrap._resolve_cli_data_home`` — a
    THIRD resolver — so on a deployed box with no ``POLYROB_DATA_DIR`` in the
    shell, ``owner invoices``/``settle``/``sub`` read a different home from the
    one every sibling owner verb (and the running service) uses, and answered a
    confident "no invoices" over a live queue. The seam is ``admin_data_dir``.
    """
    db_path_env = os.getenv("DB_PATH")
    if db_path_env and os.path.isfile(db_path_env):
        return db_path_env
    from core.db_manifest import candidate_sqlite_dbs
    for p in candidate_sqlite_dbs(_data_dir(write=None)):
        if p.name == "bot.db" and p.is_file():
            return str(p)
    return None


async def _with_bot_db(coro_factory):
    """Open the bot DB, run coro_factory(db), close. Returns (ok, result_or_msg)."""
    from pathlib import Path
    path = _bot_db_path()
    if not path:
        return False, "no bot.db found (is this instance initialized?)"
    from modules.database.connection import DatabaseConnection
    db = DatabaseConnection(Path(path))
    await db.connect()
    try:
        return True, await coro_factory(db)
    finally:
        await db.close()


def _warn_if_invoicing_off() -> None:
    """026 P0.4: pending invoices only settle while the watcher loop is on."""
    from cli._flag_warn import warn_if_flag_off

    def _enabled() -> bool:
        from modules.x402.invoicing import x402_invoicing_enabled
        return x402_invoicing_enabled()

    warn_if_flag_off(
        "X402_INVOICE_ENABLED",
        "rows are durable, but no settlement watcher runs — pending invoices "
        "will not settle or wake sessions.",
        enabled_fn=_enabled,
    )


def _warn_if_subscriptions_off() -> None:
    """026 P0.4: subscription renewals/settlement need the watcher tick."""
    from cli._flag_warn import warn_if_flag_off

    def _enabled() -> bool:
        from modules.x402.subscriptions import subscriptions_enabled
        return subscriptions_enabled()

    warn_if_flag_off(
        "SUBSCRIPTIONS_ENABLED",
        "subscription rows are durable, but renewals and settlement are not "
        "processed.",
        enabled_fn=_enabled,
    )


@owner.command("invoices")
@click.option("--user", default=None,
              help="Tenant user_id (default: this instance's owner)")
@click.option("--all-tenants", "all_tenants", is_flag=True, default=False,
              help="EVERY tenant's invoices (multi-tenant admin).")
@click.option("-n", "limit", type=int, default=50, show_default=True,
              help="How many rows to show (1-500).")
@click.option("--status", default=None,
              help="Filter by one invoice status. The vocabulary is "
                   "`modules.x402.invoicing.INVOICE_STATUSES` — pending, "
                   "settling, completed, settled_no_tx, expired, refund_due "
                   "(money taken that was not delivered on).")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def invoices(user, all_tenants, limit, status, as_json):
    """List agent-created x402 payment requests (invoices).

    Scoped to this instance's owner tenant; ask for `--all-tenants` to see
    every bucket on the box.
    """
    import asyncio
    # E22: the no-`--user` default was ALL tenants — on a multi-tenant deploy
    # that is somebody else's receivables, printed under the owner's own verb.
    tenant = None if all_tenants else _money_tenant(user)
    want = max(1, min(500, int(limit)))

    async def run(db):
        from modules.x402.invoicing import INVOICE_KIND, list_payment_requests
        if tenant:
            # C32: the tenant path is the SHARED reader — this seat used to
            # carry its own SELECT, which is how a metadata-shape fix landed in
            # one copy and not the other. Ask for one more than we show so the
            # footer can say honestly how much was cut.
            rows = await list_payment_requests(user_id=tenant, status=status,
                                               limit=want + 1, db=db)
            return {"rows": rows[:want], "more": max(0, len(rows) - want)}
        # L10 (2026-07-15): apply the --status filter IN SQL, before LIMIT 50 —
        # filtering in Python after the LIMIT silently dropped older matching rows
        # (e.g. an old pending invoice past 50 newer completed ones vanished).
        # Task 9b (2026-08-22): was `metadata LIKE '%"kind": "agent_invoice"%'` —
        # a spaced-literal match against `json.dumps`'s default spacing. ANY
        # `json_set` on this row's metadata (e.g. the boot-time subscription
        # dedup, `modules/database/x402_tables.py`) re-serializes the WHOLE
        # blob COMPACTLY (`"kind":"agent_invoice"`, no space), which the spaced
        # LIKE then silently stopped matching — dropping the row from the
        # owner's OWN invoice listing. `json_extract` reads the value
        # regardless of the blob's whitespace (mirrors the same fix already
        # applied in `modules/x402/invoicing.py`, Task 9).
        # ⚠️ The ONE query left here: the shared reader is tenant-scoped BY
        # CONTRACT (an empty user_id returns nothing), so a cross-tenant admin
        # listing has nowhere else to come from. Keep the column set identical
        # to `list_payment_requests`'s so the two views cannot drift.
        params: list = [INVOICE_KIND]
        status_clause = ""
        if status:
            status_clause = "AND status = ? "
            params.append(status)
        params.append(want + 1)
        rows = await db.fetch_all(
            "SELECT * FROM x402_payment_requests "
            "WHERE json_extract(metadata, '$.kind') = ? "
            f"{status_clause}ORDER BY created_at DESC LIMIT ?",
            tuple(params))
        import json as _json
        out = []
        for r in rows or []:
            # DatabaseConnection.fetch_* auto-parses JSON-looking TEXT columns,
            # so `metadata` may already be a dict here (json.loads(dict) raises
            # TypeError, not JSONDecodeError — must check isinstance first).
            meta = r.get("metadata")
            if not isinstance(meta, dict):
                try:
                    meta = _json.loads(meta or "{}")
                except Exception:
                    meta = {}
            out.append({"request_id": r["id"], "amount_usd": r.get("amount_usd"),
                        "status": r.get("status"), "purpose": meta.get("purpose"),
                        "payer_contact": meta.get("payer_contact") or meta.get("payer_hint"),
                        "created_at": r.get("created_at")})
        return {"rows": out[:want], "more": max(0, len(out) - want)}

    ok, result = asyncio.run(_with_bot_db(run))
    if not ok:
        if as_json:
            click.echo(json.dumps({"error": str(result)}, indent=2))
        else:
            click.echo(click.style(str(result), fg="yellow"))
        return
    rows, more = result["rows"], result["more"]
    _warn_if_invoicing_off()  # stderr — --json stdout stays machine-readable
    if as_json:
        click.echo(json.dumps({"invoices": rows, "more": more},
                              indent=2, default=str))
        return
    # M16 (2026-07-15): always print the tenant scope so the owner is never confused
    # about which bucket a listing is for.
    scope = "ALL tenants" if all_tenants else f"tenant {tenant}"
    click.echo(click.style(f"invoices — scope: {scope}"
                           + (f" · status={status}" if status else ""), dim=True))
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("invoices", "nothing has been billed from this bucket"))
        return
    for r in rows:
        color = {"pending": "yellow", "completed": "green", "expired": "red"}.get(r["status"], "white")
        # C31: `float(x or 0)` turned a NULL amount into a confident $0.00 —
        # an invoice whose amount was never recorded is UNKNOWN, not free.
        amount = r.get("amount_usd")
        try:
            shown = "$?" if amount is None else f"${float(amount):.2f}"
        except (TypeError, ValueError):
            shown = "$?"
        line = (f"{click.style(str(r['status']).ljust(10), fg=color)} "
               f"{r['request_id']}  {shown}  "
               f"{r.get('purpose') or '(no purpose)'}  ({r.get('created_at')})")
        if r.get("payer_contact"):
            line += f"  billed to: {r['payer_contact']}"
        click.echo(line)
    if more:
        click.echo(click.style(f"  ({more} more not shown — -n to raise the "
                               f"window)", dim=True))


@owner.command("settle")
@click.argument("request_id")
@click.option("--tx-hash", default=None, help="On-chain tx hash, if any")
@as_root_option
def settle(request_id, tx_hash):
    """Attest an invoice as PAID (pending -> completed).

    The originating session is woken (payment_settled) ONLY when the settlement
    watcher is actually running — i.e. ``X402_INVOICE_ENABLED=true`` AND a live
    polyrob process is up. This command flips the DB row regardless; if the
    watcher is off, the row is marked completed but no session wake fires until a
    watcher next ticks (L10)."""
    import asyncio

    async def run(db):
        from modules.x402.invoicing import settle_payment_request
        return await settle_payment_request(request_id, transaction_hash=tx_hash, db=db)

    ok, settled = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(settled), fg="yellow"))
    elif settled:
        click.echo(click.style(f"settled {request_id}", fg="green"))
        from modules.x402.invoicing import x402_invoicing_enabled
        _wake_on = x402_invoicing_enabled()
        if _wake_on:
            click.echo(click.style(
                "  the settlement watcher will wake the originating session "
                "(if a polyrob process is running).", dim=True))
        else:
            click.echo(click.style(
                "  note: X402_INVOICE_ENABLED is off — the row is completed but NO "
                "session wake will fire until a watcher runs.", fg="yellow"))
    else:
        click.echo(click.style(
            f"{request_id} not settled (unknown id or not pending)", fg="yellow"))


# --- Task 14 (Phase 3 R5): watchtower subscriptions ---------------------

@owner.group("sub")
def sub():
    """Manage watchtower subscriptions (prepaid periods gating a cron job).

    Renewal invoices + the active/grace/suspended lifecycle are driven
    automatically by the settlement watcher (SUBSCRIPTIONS_ENABLED); this
    group is read/admin only.
    """


@sub.command("list")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def sub_list(user):
    """List this tenant's watchtower subscriptions."""
    import asyncio
    # M16: money listings share ONE resolver with `polyrob finance` (resolve_identity
    # → owner-if-bound else "local"), NOT the instance-id `_owner_tenant` — so the
    # two money views can't disagree on which tenant they read.
    tenant = _money_tenant(user)

    async def run(db):
        from modules.x402 import subscriptions as subs
        return await subs.list_subscriptions(user_id=tenant, db=db)

    ok, rows = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(rows), fg="yellow"))
        return
    _warn_if_subscriptions_off()
    # M16: always print the tenant scope on the listing.
    click.echo(click.style(f"subscriptions — scope: tenant {tenant}", dim=True))
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("subscriptions", "nothing prepaid is gating a job"))
        return
    color_by_status = {"active": "green", "grace": "yellow",
                       "suspended": "red", "canceled": "white"}
    for r in rows:
        color = color_by_status.get(r["status"], "white")
        # C31: `float(r['amount_usd'])` raised TypeError on a NULL amount and
        # took the WHOLE listing down — one unpriced row hid every other
        # subscription the owner has.
        amount = r.get("amount_usd")
        try:
            price = "$?" if amount is None else f"${float(amount):.2f}"
        except (TypeError, ValueError):
            price = "$?"
        click.echo(
            f"{click.style(str(r['status']).ljust(10), fg=color)} "
            f"{r['id']}  {price}/{r.get('period_days')}d  "
            f"cron={r.get('cron_job_id')}  "
            f"{r.get('correspondent_surface')}:{r.get('correspondent_address')}  "
            f"paid_through={r.get('paid_through')}"
        )


@sub.command("cancel")
@click.argument("subscription_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@as_root_option
def sub_cancel(subscription_id, user):
    """Cancel a subscription — its cron job then $0-skips (subscription_lapsed)."""
    import asyncio
    tenant = _money_tenant(user)  # M16: same money resolver as sub_list / finance

    async def run(db):
        from modules.x402 import subscriptions as subs
        return await subs.cancel_subscription(subscription_id, user_id=tenant, db=db)

    ok, canceled = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(canceled), fg="yellow"))
        return
    if canceled:
        click.echo(click.style(f"canceled {subscription_id}", fg="green"))
    else:
        click.echo(click.style(
            f"no active subscription '{subscription_id}' for tenant {tenant}", fg="yellow"))
    _warn_if_subscriptions_off()


# --- W3: group-chat ingress allowlist (GROUP_CHAT_ENABLED) ---
# NOTE (044 C7): there is deliberately no `_group_allowlist()` helper here any
# more. Every group verb goes through `core.surfaces.group_admin`, which
# normalizes the `_100…` chat-id alias before it touches a store; a second,
# un-normalized door into `GroupAllowlist` is exactly what wrote unmatchable rows.


class _NegativeChatIdGroup(click.Group):
    """044 T18 fix round 1 (Important 5b): a real Telegram chat id is negative
    (``-1001234567890``), and Click's own parser treats a leading ``-`` on any
    positional as an option marker — ``owner groups mode telegram -1001234
    active`` fails with a bare, confusing ``Error: No such option: -1001234``
    before any command body ever runs. Catch exactly that shape here (the
    group's ``invoke`` is the one place both remedies — the ``--`` separator
    and the safe alias — can be named in the SAME message) and re-raise a
    ``UsageError`` that actually tells the operator what to do instead. Any
    other ``NoSuchOption`` (a genuine typo'd flag) is untouched.
    """

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except click.exceptions.NoSuchOption as e:
            opt = e.option_name or ""
            # Click's short-option parser only ever reports the LEADING
            # `-<digit>` of a longer negative number (e.g. `-1` out of
            # `-1001234567890`), so the caught `option_name` cannot be
            # trusted to reconstruct the operator's actual id — name both
            # remedies with a fixed illustrative example instead.
            if re.fullmatch(r"-\d+", opt):
                raise click.UsageError(
                    "A negative chat id (Telegram's real chat ids are negative, "
                    f"e.g. -1001234567890) is being parsed as an option ({opt!r}). "
                    "Either put `--` before it (e.g. `owner groups mode telegram "
                    "-- -1001234567890 active`) or use the safe alias with a "
                    "leading underscore instead of the dash "
                    "(e.g. `_1001234567890`).", ctx=ctx) from e
            raise


@owner.group("groups", cls=_NegativeChatIdGroup)
def groups():
    """Manage which group/channel chats the agent may join (default-DENY).

    A real chat id is negative (e.g. ``-1001234567890``) and needs one of two
    workarounds so Click doesn't parse it as an option: put ``--`` before it
    (``owner groups mode telegram -- -1001234567890 active``), or use the
    safe alias with a leading underscore instead of the dash
    (``owner groups mode telegram _1001234567890 active``).
    """


# 044 T18: allow/deny/list/mode/set/role/tail/service — CLI parity with the
# Telegram `/groups` seat. Every one of these calls `core.surfaces.group_admin`,
# the SAME helper set `/groups` renders through, so the two seats can never
# disagree on what a verb does or how it reports back.
#
# ⚠️ 044 C7: allow/deny/list used to talk to `GroupAllowlist` DIRECTLY, bypassing
# `group_admin`'s `_norm_chat_id` — so the `_1001234567890` alias this very group
# advertises wrote a row under the LITERAL `_100…` string. That row matched
# nothing at routing time (which sees the real `-100…`), and `list` then reported
# it as an active room: the owner was told a room was allowed while the agent
# went on dropping every line from it.

@groups.command("allow")
@click.argument("surface")
@click.argument("chat_id")
@click.option("--note", default="", help="Label, e.g. 'dev server #general'")
@as_root_option
def groups_allow(surface, chat_id, note):
    """Allow a group chat: polyrob owner groups allow discord <channel_id>.

    CHAT_ID is negative for Telegram — put `--` before it or use the safe
    `_` alias (see `polyrob owner groups --help`).
    """
    from core.surfaces import group_admin
    click.echo(group_admin.allow_here(_group_container(write=True), surface, chat_id, note,
                                      owner_uid=_group_owner_uid()))


@groups.command("deny")
@click.argument("surface")
@click.argument("chat_id")
@as_root_option
def groups_deny(surface, chat_id):
    """Revoke a group chat.

    CHAT_ID is negative for Telegram — put `--` before it or use the safe
    `_` alias (see `polyrob owner groups --help`).
    """
    from core.surfaces import group_admin
    click.echo(group_admin.deny_here(_group_container(write=True), surface, chat_id,
                                     owner_uid=_group_owner_uid()))


@groups.command("list")
def groups_list():
    """List group-chat allowlist entries."""
    from core.surfaces import group_admin
    click.echo(group_admin.list_rooms(_group_container(write=False), _group_owner_uid()))


def _group_container(*, write: "bool | None" = None):
    """A minimal `container` for `core.surfaces.group_admin`: just enough for
    it to resolve `data_dir` — the CLI never installs the surface bus, so
    `group_admin` falls back to opening its own handle on `surfaces.db`.

    *write* is the euid-guard intent (C40), threaded from the calling verb."""
    import types
    return types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=_data_dir(write=write)),
        get_service=lambda name: None)


def _group_owner_uid() -> str:
    """The tenant a room's overlay lives under — the ONE owner-tenant resolver,
    so this CLI seat and the Telegram `/groups` seat (`group_ops._owner_uid`) and
    the read side (`chat_policy.load_for_chat`) cannot name different buckets."""
    from core.instance import resolve_owner_user_id
    return resolve_owner_user_id()


@groups.command("mode")
@click.argument("surface")
@click.argument("chat_id")
@click.argument("mode")
@as_root_option
def groups_mode(surface, chat_id, mode):
    """Set a room's chat.mode: mention|active|listen|off.

    CHAT_ID is negative for Telegram — put `--` before it or use the safe
    `_` alias (see `polyrob owner groups --help`).
    """
    from core.surfaces import group_admin
    click.echo(group_admin.set_mode(_group_container(write=True), _group_owner_uid(),
                                    surface, chat_id, mode))


@groups.command("set")
@click.argument("surface")
@click.argument("chat_id")
@click.argument("key")
@click.argument("value")
@as_root_option
def groups_set(surface, chat_id, key, value):
    """Set one chat.* key on a room (value 'unset' or '-' clears it).

    CHAT_ID is negative for Telegram — put `--` before it or use the safe
    `_` alias (see `polyrob owner groups --help`).
    """
    from core.surfaces import group_admin
    click.echo(group_admin.set_key(_group_container(write=True), _group_owner_uid(),
                                   surface, chat_id, key, value))


@groups.command("role")
@click.argument("surface")
@click.argument("chat_id")
@click.argument("user_id")
@click.argument("role")
@as_root_option
def groups_role(surface, chat_id, user_id, role):
    """Grant a per-chat role: admin|member|blocked.

    CHAT_ID is negative for Telegram — put `--` before it or use the safe
    `_` alias (see `polyrob owner groups --help`). USER_ID must be the raw
    numeric platform id (see `/groups admins here` on Telegram) — a handle
    can never be resolved to one.
    """
    from core.surfaces import group_admin
    click.echo(group_admin.set_role(_group_container(write=True), surface, chat_id, user_id,
                                    role, by="cli"))


@groups.command("admins")
@click.argument("surface")
@click.argument("chat_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def groups_admins(surface, chat_id, as_json):
    """Who holds a ROLE in a room (admin / member / blocked).

    ⚠️ These are the roles POLYROB recorded, not the platform's own admin
    list. A chat's real admins can only be read through a live connection to
    that surface — ask `/groups admins here` on Telegram for that, then grant
    a role here with `polyrob owner groups role`.

    CHAT_ID is negative for Telegram — put `--` before it or use the safe
    `_` alias (see `polyrob owner groups --help`).
    """
    import os as _os

    from core.surfaces.group_admin import normalize_chat_id
    from core.surfaces.group_roles import GroupRoles
    chat = normalize_chat_id(chat_id)
    db = _os.path.join(_data_dir(write=False), "surfaces.db")
    if not _os.path.exists(db):
        # A read never CREATES a store: an absent file is "nothing recorded",
        # and saying so beats opening one so the answer can be an empty list.
        msg = (f"no surface store yet ({db}) — no room role has ever been "
               f"granted on this box.")
        click.echo(json.dumps({"error": msg}, indent=2) if as_json
                   else click.style(msg, fg="yellow"))
        return
    try:
        rows = GroupRoles(db).list(surface, chat)
    except Exception as exc:
        raise click.ClickException(
            f"the role store could not be read ({type(exc).__name__}: {exc}). "
            f"That is UNKNOWN, not 'nobody has a role here'.")
    if as_json:
        click.echo(json.dumps({"surface": surface, "chat_id": chat,
                               "roles": rows}, indent=2, default=str))
        return
    click.echo(click.style(f"roles in {surface}:{chat}", bold=True))
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("roles granted here",
                         "every member is a plain member; the bound owner is "
                         "always owner"))
    for r in rows:
        click.echo(f"  {str(r.get('role')).ljust(8)} {r.get('user_id')}"
                   f"   (by {r.get('granted_by') or '?'})")
    click.echo(click.style(
        "\nreal platform admins: ask `/groups admins here` on the surface "
        "itself — the CLI has no live connection to it.", dim=True))


@groups.command("tail")
@click.argument("surface")
@click.argument("chat_id")
@click.option("-n", "--limit", default=30, help="How many lines")
def groups_tail(surface, chat_id, limit):
    """Show the last N ledger lines for a room.

    CHAT_ID is negative for Telegram — put `--` before it or use the safe
    `_` alias (see `polyrob owner groups --help`).
    """
    from core.surfaces import group_admin
    click.echo(group_admin.tail(_group_container(write=False), surface, chat_id, limit))


@groups.command("service")
@click.argument("surface")
@click.argument("chat_id")
@click.option("--every", default="30m",
              help="Cadence, e.g. 30m; 'off' stops the job")
@click.option("--max", "max_replies", default=3, help="Max replies per run")
@as_root_option
def groups_service(surface, chat_id, every, max_replies):
    """Start (or, with --every off, stop) the recurring job that services a room.

    The job reads the room's ledger since its own checkpoint and answers only
    what needs answering; an empty tail costs nothing. CHAT_ID is negative for
    Telegram — put `--` before it or use the safe `_` alias (see
    `polyrob owner groups --help`).
    """
    from cron.room_service import service as _room_service
    click.echo(_room_service(_group_container(write=True), _group_owner_uid(),
                             surface, chat_id, every=every, max_replies=max_replies))


# ---------------------------------------------------------------------------
# DM pairing (POLYROB_REQUIRE_PAIRING) — 3.11/O5, 2026-07-14 review.
# core/pairing.py issues one-time codes to unknown senders; these commands are
# the operator-side approval path that previously DIDN'T EXIST (the docstring
# pointed at a phantom `rob pair approve`). Same PairingStore + data home the
# surface dispatcher uses.
# ---------------------------------------------------------------------------

def _pairing_store(*, write: "bool | None" = None):
    import os as _os

    from core.pairing import PairingStore
    return PairingStore(_os.path.join(_data_dir(write=write), "pairing.db"))


@owner.group("pair")
def pair():
    """Approve/inspect DM pairing requests (POLYROB_REQUIRE_PAIRING)."""


@pair.command("pending")
def pair_pending():
    """List users waiting for pairing approval (with their codes)."""
    rows = _pairing_store(write=False).list_pending()
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("pending pairing requests",
                         "nobody unknown has written in"))
        return
    for user_id, code in rows:
        click.echo(f"pending  {user_id}  code={code}"
                   "  (approve: polyrob owner pair approve <code>)")


@pair.command("approve")
@click.argument("code")
@as_root_option
def pair_approve(code):
    """Approve the pairing request holding CODE."""
    uid = _pairing_store(write=True).approve(code)
    if uid is None:
        raise click.ClickException(f"no pending pairing request with code {code!r}")
    click.echo(click.style(f"paired {uid}", fg="green"))


@pair.command("revoke")
@click.argument("user_id")
@as_root_option
def pair_revoke(user_id):
    """Revoke a paired (or pending) user."""
    _pairing_store(write=True).revoke(user_id)
    click.echo(click.style(f"revoked {user_id}", fg="green"))


# ---------------------------------------------------------------------------
# 046: paid room actions
# ---------------------------------------------------------------------------

@owner.group("paid")
def paid():
    """Paid room actions — what rooms sell, and what is OWED.

    Renders the SAME `core.surfaces.room_action_admin` helpers the Telegram
    `/paid` seat does, so the two can never disagree about a room.
    """


@paid.command("list")
@click.argument("chat_id_arg", metavar="[CHAT_ID]", required=False, default=None)
@click.option("--surface", default="telegram", show_default=True)
@click.option("--chat", "chat_id", default=None,
              help="(alias of the positional CHAT_ID) a room's chat id; omit to list credits owed across rooms")
def paid_list(chat_id_arg, surface, chat_id):
    """Recent offers in a room (CHAT_ID, like every other `paid` verb), or every credit owed."""
    from core.surfaces import room_action_admin as adm
    chat_id = chat_id_arg if chat_id_arg is not None else chat_id
    if chat_id is None:
        click.echo(adm.render_credits(_group_container(write=False)))
        return
    from core.surfaces.group_admin import normalize_chat_id
    click.echo(adm.offers(_group_container(write=False), surface,
                          normalize_chat_id(chat_id)))


@paid.command("show")
@click.option("--surface", default="telegram", show_default=True)
@click.argument("chat_id")
def paid_show(surface, chat_id):
    """What this room sells, at what price, in what asset."""
    from core.surfaces import room_action_admin as adm
    from core.surfaces.group_admin import normalize_chat_id
    # ⚠️ The CLI remedy table: this seat prints at a SHELL prompt, where
    # `/paid price …` and `/groups set here …` are not commands. One table per
    # seat (`core/surfaces/room_action_admin.py`), never a second sentence.
    click.echo(adm.status(_group_container(write=False), surface,
                          normalize_chat_id(chat_id),
                          remedies=adm.CLI_REMEDIES))


@paid.command("enable")
@click.option("--surface", default="telegram", show_default=True)
@click.argument("chat_id")
@as_root_option
def paid_enable(surface, chat_id):
    """Turn paid actions ON in a room (refuses while nothing is priced)."""
    from core.surfaces import room_action_admin as adm
    from core.surfaces.group_admin import normalize_chat_id
    click.echo(adm.enable(_group_container(write=True), surface,
                          normalize_chat_id(chat_id),
                          remedies=adm.CLI_REMEDIES))


@paid.command("disable")
@click.option("--surface", default="telegram", show_default=True)
@click.argument("chat_id")
@as_root_option
def paid_disable(surface, chat_id):
    """Turn paid actions OFF in a room — a member's verb is refused, not priced."""
    from core.surfaces import room_action_admin as adm
    from core.surfaces.group_admin import normalize_chat_id
    click.echo(adm.disable(_group_container(write=True), surface,
                           normalize_chat_id(chat_id)))


@paid.command("price")
@click.option("--surface", default="telegram", show_default=True)
@click.argument("chat_id")
@click.argument("verb")
@click.argument("usd", type=float)
@as_root_option
def paid_price(surface, chat_id, verb, usd):
    """Price one verb in a room: polyrob owner paid price <chat> mute 0.50"""
    from core.surfaces import room_action_admin as adm
    from core.surfaces.group_admin import normalize_chat_id
    click.echo(adm.set_price(_group_container(write=True), surface,
                             normalize_chat_id(chat_id), verb, usd,
                             remedies=adm.CLI_REMEDIES))


@paid.command("asset")
@click.option("--surface", default="telegram", show_default=True)
@click.argument("chat_id")
@click.argument("asset_id")
@as_root_option
def paid_asset(surface, chat_id, asset_id):
    """Set the asset a room is paid in (must already be pinned by `wallet asset add`)."""
    from core.surfaces import room_action_admin as adm
    from core.surfaces.group_admin import normalize_chat_id
    click.echo(adm.set_asset(_group_container(write=True), surface,
                             normalize_chat_id(chat_id), asset_id))


@paid.command("offers")
@click.option("--surface", default="telegram", show_default=True)
@click.argument("chat_id")
@click.option("-n", "limit", type=int, default=10, show_default=True)
def paid_offers(surface, chat_id, limit):
    """Recent offers in one room, newest first."""
    from core.surfaces import room_action_admin as adm
    from core.surfaces.group_admin import normalize_chat_id
    click.echo(adm.offers(_group_container(write=False), surface,
                          normalize_chat_id(chat_id), limit=max(1, int(limit))))


@paid.command("cancel")
@click.argument("offer_id")
@as_root_option
def paid_cancel(offer_id):
    """Withdraw a PENDING offer.

    A paid offer cannot be cancelled — that case is a credit, not a
    cancellation.
    """
    from core.surfaces import room_action_admin as adm
    click.echo(adm.cancel(_group_container(write=True), offer_id, by="cli"))
